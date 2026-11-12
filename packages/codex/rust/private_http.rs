//! Private HTTP keeps live connections but reloads default trust for new handshakes.
use std::sync::{Arc, OnceLock};

use rustls::client::WebPkiServerVerifier;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{CryptoProvider, verify_tls12_signature, verify_tls13_signature};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{ClientConfig, DigitallySignedStruct, Error, RootCertStore, SignatureScheme};

#[derive(Debug)]
struct CurrentDefaultRoots {
    provider: Arc<CryptoProvider>,
    bundle_first: bool,
    #[cfg(all(test, target_os = "linux"))]
    roots_path: Option<std::path::PathBuf>,
    #[cfg(all(test, target_os = "linux"))]
    roots_dir: Option<std::path::PathBuf>,
}

impl CurrentDefaultRoots {
    fn load(&self) -> rustls_native_certs::CertificateResult {
        #[cfg(all(test, target_os = "linux"))]
        if let Some(path) = &self.roots_path {
            return rustls_native_certs::load_certs_from_paths(
                Some(path),
                self.roots_dir.as_deref(),
            );
        }
        rustls_native_certs::load_native_certs()
    }

    fn bundle(&self) -> Option<rustls_native_certs::CertificateResult> {
        #[cfg(all(test, target_os = "linux"))]
        if let Some(path) = &self.roots_path {
            return Some(rustls_native_certs::load_certs_from_paths(Some(path), None));
        }
        #[cfg(target_os = "linux")]
        return openssl_probe::probe()
            .cert_file
            .map(|path| rustls_native_certs::load_certs_from_paths(Some(&path), None));
        #[cfg(not(target_os = "linux"))]
        None
    }

    fn stock(
        &self,
        certs: Vec<CertificateDer<'static>>,
    ) -> Result<Arc<WebPkiServerVerifier>, Error> {
        let total = certs.len();
        let mut roots = RootCertStore::empty();
        let (added, ignored) = roots.add_parsable_certificates(certs);
        tracing::debug!("Added {added}/{total} native root certificates (ignored {ignored})");
        if self.bundle_first && roots.is_empty() {
            return Err(Error::InvalidCertificate(
                rustls::CertificateError::UnknownIssuer,
            ));
        }
        WebPkiServerVerifier::builder_with_provider(Arc::new(roots), self.provider.clone())
            .build()
            .map_err(|error| Error::General(error.to_string()))
    }
}

impl ServerCertVerifier for CurrentDefaultRoots {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, Error> {
        // The native loader honors these selectors. A late change must not turn
        // this admitted default-trust connection into a custom-CA connection.
        if ["CODEX_CA_CERTIFICATE", "SSL_CERT_DIR", "SSL_CERT_FILE"]
            .iter()
            .any(|name| std::env::var_os(name).is_some())
        {
            return Err(Error::General(
                "private HTTP default trust profile changed".into(),
            ));
        }
        if self.bundle_first {
            if let Some(bundle) = self.bundle() {
                if bundle.errors.is_empty() {
                    if let Ok(verifier) = self.stock(bundle.certs) {
                        if let Ok(success) = verifier.verify_server_cert(
                            end_entity,
                            intermediates,
                            server_name,
                            ocsp_response,
                            now,
                        ) {
                            return Ok(success);
                        }
                    }
                }
            }
        }
        let rustls_native_certs::CertificateResult { certs, errors, .. } = self.load();
        // Match the pinned default-WSS loader: warn on individual source errors
        // while using its current partial result, never a retained verifier.
        if !errors.is_empty() {
            if self.bundle_first {
                tracing::warn!(target: "codex_http_client::custom_ca", native_root_error_count = errors.len(),
                    "encountered errors while loading native root certificates");
            } else {
                tracing::warn!("native root CA certificate loading errors: {errors:?}");
            }
        }
        if self.bundle_first && certs.is_empty() {
            return Err(Error::InvalidCertificate(
                rustls::CertificateError::UnknownIssuer,
            ));
        }
        if certs.is_empty() {
            return Err(Error::General(format!(
                "no native root CA certificates found (errors: {errors:?})"
            )));
        }
        self.stock(certs)?.verify_server_cert(
            end_entity,
            intermediates,
            server_name,
            ocsp_response,
            now,
        )
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        verify_tls12_signature(
            message,
            cert,
            signature,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        verify_tls13_signature(
            message,
            cert,
            signature,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider
            .signature_verification_algorithms
            .supported_schemes()
    }
}

fn connection_config(verifier: Arc<CurrentDefaultRoots>) -> ClientConfig {
    ClientConfig::builder_with_provider(verifier.provider.clone())
        .with_safe_default_protocol_versions()
        .expect("installed provider supports default TLS versions")
        .dangerous()
        .with_custom_certificate_verifier(verifier)
        .with_no_client_auth()
}

fn config(verifier: Arc<CurrentDefaultRoots>) -> ClientConfig {
    let mut config = connection_config(verifier);
    // use_preconfigured_tls bypasses Reqwest's normal TLS setup. Keep its
    // Rustls default SNI, ALPN and protocol policy, without session resumption.
    config.enable_sni = true;
    config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    config.resumption = rustls::client::Resumption::disabled();
    config
}

// Process-owned transport; fresh request headers and the existing
// per-Engine/conversation retirement custody.
pub(crate) fn process_pool() -> codex_http_client::SdkModelHttpPool {
    static POOL: OnceLock<codex_http_client::SdkModelHttpPool> = OnceLock::new();
    POOL.get_or_init(pool).clone()
}

pub(crate) fn pool() -> codex_http_client::SdkModelHttpPool {
    codex_utils_rustls_provider::ensure_rustls_crypto_provider();
    let verifier = Arc::new(CurrentDefaultRoots {
        provider: CryptoProvider::get_default()
            .expect("crypto provider initialized")
            .clone(),
        bundle_first: false,
        #[cfg(all(test, target_os = "linux"))]
        roots_path: None,
        #[cfg(all(test, target_os = "linux"))]
        roots_dir: None,
    });
    codex_http_client::SdkModelHttpPool::with_preconfigured_tls(config(verifier))
}

#[cfg(all(test, target_os = "linux"))]
#[path = "private_http_tls_tests.rs"]
mod tests;

// A new config retains WSS defaults: empty ALPN and an unshared resumption store.
pub(crate) fn websocket_config() -> ClientConfig {
    connection_config(Arc::new(CurrentDefaultRoots {
        provider: CryptoProvider::get_default()
            .expect("crypto provider initialized")
            .clone(),
        bundle_first: true,
        #[cfg(all(test, target_os = "linux"))]
        roots_path: None,
        #[cfg(all(test, target_os = "linux"))]
        roots_dir: None,
    }))
}
