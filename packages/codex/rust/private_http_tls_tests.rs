//! Synthetic TLS proves socket reuse and current trust on each new connection.
//! PEM fixtures match test_sdk_session_tls.py; no production trust-source override.
use super::*;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::Duration;

use codex_http_client::{HttpClientFactory, OutboundProxyPolicy, SdkModelHttpPool};
use rustls::pki_types::{PrivateKeyDer, pem::PemObject};

const CA_A: &[u8] = include_bytes!("fixtures/private-http-tls/ca-A.pem");
const CA_B: &[u8] = include_bytes!("fixtures/private-http-tls/ca-B.pem");
const CERT_A: &[u8] = include_bytes!("fixtures/private-http-tls/server-A.pem");
const KEY_A: &[u8] = include_bytes!("fixtures/private-http-tls/server-A.key");
const CERT_B: &[u8] = include_bytes!("fixtures/private-http-tls/server-B.pem");
const KEY_B: &[u8] = include_bytes!("fixtures/private-http-tls/server-B.key");

fn isolated(name: &str, selector: Option<&str>) -> bool {
    if std::env::var("SDK_PRIVATE_HTTP_TEST").as_deref() == Ok(name) {
        return true;
    }
    let mut command = std::process::Command::new(std::env::current_exe().unwrap());
    command.args([
        "--exact",
        &format!("private_http::tests::{name}"),
        "--nocapture",
    ]);
    command.env("SDK_PRIVATE_HTTP_TEST", name);
    // No process-wide mutations in parallel Rust tests or production selectors.
    for key in [
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "REQUEST_METHOD",
        "CODEX_SANDBOX",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "LD_PRELOAD",
        "LD_AUDIT",
    ] {
        command.env_remove(key);
    }
    if let Some(selector) = selector {
        command.env(selector, "");
    }
    assert!(
        command
            .status()
            .expect("join isolated native test")
            .success()
    );
    false
}

fn verifier(path: &Path) -> Arc<CurrentDefaultRoots> {
    codex_utils_rustls_provider::ensure_rustls_crypto_provider();
    Arc::new(CurrentDefaultRoots {
        provider: CryptoProvider::get_default().unwrap().clone(),
        bundle_first: false,
        roots_path: Some(path.to_owned()),
        roots_dir: None,
    })
}

struct Peer {
    address: SocketAddr,
    stop: Arc<AtomicBool>,
    requests: Arc<AtomicUsize>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl Peer {
    fn new(cert: &[u8], key: &[u8]) -> Self {
        let provider = CryptoProvider::get_default().unwrap().clone();
        let cert = CertificateDer::from_pem_slice(cert).unwrap();
        let key = PrivateKeyDer::from_pem_slice(key).unwrap();
        let mut server = rustls::ServerConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(vec![cert], key)
            .unwrap();
        server.alpn_protocols = vec![b"http/1.1".to_vec()];
        let server = Arc::new(server);
        let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let address = listener.local_addr().unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let requests = Arc::new(AtomicUsize::new(0));
        let stopped = stop.clone();
        let observed = requests.clone();
        let thread = std::thread::spawn(move || {
            let mut connection_id = 0;
            while !stopped.load(Ordering::Acquire) {
                let (socket, _) = match listener.accept() {
                    Ok(socket) => socket,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(5));
                        continue;
                    }
                    Err(error) => panic!("owned TLS listener: {error}"),
                };
                connection_id += 1;
                socket
                    .set_read_timeout(Some(Duration::from_millis(100)))
                    .unwrap();
                socket
                    .set_write_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                let session = rustls::ServerConnection::new(server.clone()).unwrap();
                let mut stream = rustls::StreamOwned::new(session, socket);
                let mut request = Vec::new();
                while !stopped.load(Ordering::Acquire) {
                    let mut byte = [0];
                    match stream.read(&mut byte) {
                        Ok(0) => break,
                        Ok(_) => request.push(byte[0]),
                        Err(error)
                            if matches!(
                                error.kind(),
                                std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                            ) =>
                        {
                            continue;
                        }
                        // Expected for a client rejecting the server's certificate.
                        Err(_) => break,
                    }
                    assert!(request.len() < 16 * 1024, "bounded synthetic headers");
                    if !request.ends_with(b"\r\n\r\n") {
                        continue;
                    }
                    observed.fetch_add(1, Ordering::AcqRel);
                    let close = request.starts_with(b"GET /close ");
                    let body = connection_id.to_string();
                    let connection = if close { "close" } else { "keep-alive" };
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: {connection}\r\n\r\n{body}",
                        body.len()
                    );
                    stream.write_all(response.as_bytes()).unwrap();
                    stream.flush().unwrap();
                    request.clear();
                    if close {
                        break;
                    }
                }
            }
        });
        Self {
            address,
            stop,
            requests,
            thread: Some(thread),
        }
    }

    fn url(&self, path: &str) -> String {
        format!("https://localhost:{}{path}", self.address.port())
    }
}

impl Drop for Peer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        self.thread.take().unwrap().join().unwrap();
    }
}

async fn fetch(pool: &SdkModelHttpPool, peer: &Peer, path: &str) -> Result<String, String> {
    let url = peer.url(path);
    let client = pool
        .client(
            &HttpClientFactory::new(OutboundProxyPolicy::ReqwestDefault),
            &url,
            false,
            http::HeaderMap::new(),
        )
        .map_err(|error| error.to_string())?
        .expect("direct profile admitted");
    let response = client
        .get(url)
        .timeout(Duration::from_secs(3))
        .send()
        .await
        .map_err(|error| error.to_string())?;
    response.text().await.map_err(|error| error.to_string())
}

#[test]
fn live_connections_reuse_but_new_connections_load_current_roots() {
    if !isolated(
        "live_connections_reuse_but_new_connections_load_current_roots",
        None,
    ) {
        return;
    }
    let directory = tempfile::TempDir::new().unwrap();
    let path = directory.path().join("current-roots.pem");
    std::fs::write(&path, CA_A).unwrap();
    let pool = SdkModelHttpPool::with_preconfigured_tls(config(verifier(&path)));
    let first = Peer::new(CERT_A, KEY_A);
    let second = Peer::new(CERT_B, KEY_B);
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            let socket = fetch(&pool, &first, "/first").await.unwrap();
            assert_eq!(socket, fetch(&pool, &first, "/second").await.unwrap());
            std::fs::write(&path, CA_B).unwrap();
            // An established authenticated socket remains valid; this is not revocation.
            assert_eq!(socket, fetch(&pool, &first, "/close").await.unwrap());
            assert!(fetch(&pool, &first, "/removed-root").await.is_err());
            assert_eq!(first.requests.load(Ordering::Acquire), 3);
            assert!(fetch(&pool, &second, "/close").await.is_ok());
            std::fs::remove_file(&path).unwrap();
            assert!(fetch(&pool, &first, "/missing-roots").await.is_err());
            std::fs::write(&path, CA_A).unwrap();
            assert_ne!(socket, fetch(&pool, &first, "/close").await.unwrap());
        });
    pool.close();
    assert!(
        pool.client(
            &HttpClientFactory::new(OutboundProxyPolicy::ReqwestDefault),
            &first.url("/"),
            false,
            http::HeaderMap::new()
        )
        .is_err()
    );
}

#[test]
fn partial_roots_preserve_hostname_and_current_time_checks() {
    if !isolated(
        "partial_roots_preserve_hostname_and_current_time_checks",
        None,
    ) {
        return;
    }
    let directory = tempfile::TempDir::new().unwrap();
    let path = directory.path().join("roots.pem");
    let mut partial = CA_A.to_vec();
    partial
        .extend_from_slice(b"-----BEGIN CERTIFICATE-----\n!invalid!\n-----END CERTIFICATE-----\n");
    std::fs::write(&path, partial).unwrap();
    let verifier = verifier(&path);
    let cert = CertificateDer::from_pem_slice(CERT_A).unwrap();
    let localhost = ServerName::try_from("localhost").unwrap();
    assert!(
        verifier
            .verify_server_cert(&cert, &[], &localhost, &[], UnixTime::now())
            .is_ok()
    );
    let wrong = ServerName::try_from("wrong.example").unwrap();
    assert!(
        verifier
            .verify_server_cert(&cert, &[], &wrong, &[], UnixTime::now())
            .is_err()
    );
    assert!(
        verifier
            .verify_server_cert(
                &cert,
                &[],
                &localhost,
                &[],
                UnixTime::since_unix_epoch(Duration::ZERO)
            )
            .is_err()
    );
    assert!(
        verifier
            .verify_server_cert(
                &cert,
                &[],
                &localhost,
                &[],
                UnixTime::since_unix_epoch(Duration::from_secs(2_200_000_000))
            )
            .is_err()
    );
}

#[test]
fn empty_late_trust_selector_refuses_verification() {
    if !isolated(
        "empty_late_trust_selector_refuses_verification",
        Some("SSL_CERT_FILE"),
    ) {
        return;
    }
    let directory = tempfile::TempDir::new().unwrap();
    let path = directory.path().join("roots.pem");
    std::fs::write(&path, CA_A).unwrap();
    let pool = SdkModelHttpPool::with_preconfigured_tls(config(verifier(&path)));
    assert!(
        pool.client(
            &HttpClientFactory::new(OutboundProxyPolicy::ReqwestDefault),
            "https://api.openai.com/v1/responses",
            false,
            http::HeaderMap::new(),
        )
        .unwrap()
        .is_none()
    );
    let cert = CertificateDer::from_pem_slice(CERT_A).unwrap();
    assert!(
        verifier(&path)
            .verify_server_cert(
                &cert,
                &[],
                &ServerName::try_from("localhost").unwrap(),
                &[],
                UnixTime::now()
            )
            .is_err()
    );
}

#[test]
fn bundle_first_retained_verifier_observes_rotation_removal_and_invalid_peers() {
    if !isolated(
        "bundle_first_retained_verifier_observes_rotation_removal_and_invalid_peers",
        None,
    ) {
        return;
    }
    let directory = tempfile::TempDir::new().unwrap();
    let path = directory.path().join("current-roots.pem");
    std::fs::write(&path, CA_A).unwrap();
    codex_utils_rustls_provider::ensure_rustls_crypto_provider();
    let verifier = CurrentDefaultRoots {
        provider: CryptoProvider::get_default().unwrap().clone(),
        bundle_first: true,
        roots_path: Some(path.clone()),
        roots_dir: None,
    };
    let a = CertificateDer::from_pem_slice(CERT_A).unwrap();
    let b = CertificateDer::from_pem_slice(CERT_B).unwrap();
    let host = ServerName::try_from("localhost").unwrap();
    let now = UnixTime::now();
    assert!(
        verifier
            .verify_server_cert(&a, &[], &host, &[], now)
            .is_ok()
    );
    std::fs::write(&path, CA_B).unwrap();
    assert!(
        verifier
            .verify_server_cert(&a, &[], &host, &[], now)
            .is_err()
    );
    assert!(
        verifier
            .verify_server_cert(&b, &[], &host, &[], now)
            .is_ok()
    );
    std::fs::remove_file(&path).unwrap();
    assert!(
        verifier
            .verify_server_cert(&b, &[], &host, &[], now)
            .is_err()
    );
    std::fs::write(&path, CA_A).unwrap();
    assert!(
        verifier
            .verify_server_cert(&a, &[], &host, &[], now)
            .is_ok()
    );
    assert!(
        verifier
            .verify_server_cert(
                &a,
                &[],
                &ServerName::try_from("wrong.example").unwrap(),
                &[],
                now
            )
            .is_err()
    );
    assert!(
        verifier
            .verify_server_cert(
                &a,
                &[],
                &host,
                &[],
                UnixTime::since_unix_epoch(Duration::ZERO)
            )
            .is_err()
    );
    let mut damaged = a.as_ref().to_vec();
    let last = damaged.len() - 1;
    damaged[last] ^= 1;
    assert!(
        verifier
            .verify_server_cert(&CertificateDer::from(damaged), &[], &host, &[], now)
            .is_err()
    );
}

#[test]
fn websocket_selector_returns_fresh_wss_configs_only() {
    if !isolated("websocket_selector_returns_fresh_wss_configs_only", None) {
        return;
    }
    let url = url::Url::parse("wss://api.openai.com/v1/responses").unwrap();
    let first = crate::private_wss::client_config(&url).unwrap();
    let second = crate::private_wss::client_config(&url).unwrap();
    assert!(!Arc::ptr_eq(&first, &second));
    assert!(first.alpn_protocols.is_empty() && second.alpn_protocols.is_empty());
    assert!(first.enable_sni && second.enable_sni);
    assert!(
        crate::private_wss::client_config(
            &url::Url::parse("wss://example.invalid/v1/responses").unwrap()
        )
        .is_none()
    );
    assert!(
        crate::private_wss::client_config(
            &url::Url::parse("https://api.openai.com/v1/responses").unwrap()
        )
        .is_none()
    );
}

#[test]
fn websocket_selector_leaves_explicit_ca_with_original_connector() {
    if !isolated(
        "websocket_selector_leaves_explicit_ca_with_original_connector",
        Some("SSL_CERT_FILE"),
    ) {
        return;
    }
    assert!(
        crate::private_wss::client_config(
            &url::Url::parse("wss://api.openai.com/v1/responses").unwrap()
        )
        .is_none()
    );
}

#[test]
fn bundle_first_uses_unhashed_directory_fallback_and_observes_removal() {
    if !isolated(
        "bundle_first_uses_unhashed_directory_fallback_and_observes_removal",
        None,
    ) {
        return;
    }
    let directory = tempfile::TempDir::new().unwrap();
    let bundle = directory.path().join("bundle.pem");
    let roots_dir = directory.path().join("unhashed");
    std::fs::create_dir(&roots_dir).unwrap();
    let b_root = roots_dir.join("arbitrary-root-name.pem");
    std::fs::write(&bundle, CA_A).unwrap();
    std::fs::write(&b_root, CA_B).unwrap();
    codex_utils_rustls_provider::ensure_rustls_crypto_provider();
    let verifier = CurrentDefaultRoots {
        provider: CryptoProvider::get_default().unwrap().clone(),
        bundle_first: true,
        roots_path: Some(bundle),
        roots_dir: Some(roots_dir),
    };
    let a = CertificateDer::from_pem_slice(CERT_A).unwrap();
    let b = CertificateDer::from_pem_slice(CERT_B).unwrap();
    let host = ServerName::try_from("localhost").unwrap();
    let now = UnixTime::now();
    assert!(
        verifier
            .verify_server_cert(&a, &[], &host, &[], now)
            .is_ok()
    );
    assert!(
        verifier
            .verify_server_cert(&b, &[], &host, &[], now)
            .is_ok()
    );
    let mut damaged = b.as_ref().to_vec();
    let last = damaged.len() - 1;
    damaged[last] ^= 1;
    assert!(
        verifier
            .verify_server_cert(&CertificateDer::from(damaged), &[], &host, &[], now)
            .is_err()
    );
    std::fs::remove_file(&b_root).unwrap();
    assert!(
        verifier
            .verify_server_cert(&b, &[], &host, &[], now)
            .is_err()
    );
    assert!(
        verifier
            .verify_server_cert(&a, &[], &host, &[], now)
            .is_ok()
    );
    std::fs::write(&b_root, CA_B).unwrap();
    assert!(
        verifier
            .verify_server_cert(&b, &[], &host, &[], now)
            .is_ok()
    );
}
