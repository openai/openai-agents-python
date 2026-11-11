#include <CommonCrypto/CommonDigest.h>
#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

static void require_status(const char *operation, OSStatus status) {
    if (status != errSecSuccess) {
        fprintf(stderr, "%s failed: %d\n", operation, (int)status);
        exit(2);
    }
}

int main(int argc, char **argv) {
    if (argc < 4) {
        return 2;
    }

    CFMutableArrayRef certificates = CFArrayCreateMutable(NULL, 0, &kCFTypeArrayCallBacks);
    for (int i = 3; i < argc; i++) {
        unsigned char der[32768];
        FILE *file = fopen(argv[i], "rb");
        if (!file) {
            perror("open DER");
            return 2;
        }
        size_t size = fread(der, 1, sizeof(der), file);
        if (!size || !feof(file) || ferror(file)) {
            fprintf(stderr, "invalid DER input length or read error\n");
            return 2;
        }
        fclose(file);
        CFDataRef data = CFDataCreate(NULL, der, (CFIndex)size);
        SecCertificateRef certificate = SecCertificateCreateWithData(NULL, data);
        if (!certificate) {
            fprintf(stderr, "cannot parse DER certificate %d\n", i - 3);
            return 2;
        }
        CFArrayAppendValue(certificates, certificate);
        CFRelease(certificate);
        CFRelease(data);

        unsigned char digest[CC_SHA256_DIGEST_LENGTH];
        CC_SHA256(der, (CC_LONG)size, digest);
        printf("DER %d ", i - 3);
        for (size_t j = 0; j < sizeof(digest); j++) {
            printf("%02x", (unsigned int)digest[j]);
        }
        putchar('\n');
    }

    CFStringRef hostname = CFStringCreateWithCString(NULL, argv[1], kCFStringEncodingASCII);
    SecPolicyRef policy = SecPolicyCreateSSL(true, hostname);
    SecTrustRef trust = NULL;
    require_status("SecTrustCreateWithCertificates",
                   SecTrustCreateWithCertificates(certificates, policy, &trust));
    CFDateRef date = CFDateCreate(NULL, strtod(argv[2], NULL) - kCFAbsoluteTimeIntervalSince1970);
    require_status("SecTrustSetVerifyDate", SecTrustSetVerifyDate(trust, date));
    require_status("SecTrustSetNetworkFetchAllowed", SecTrustSetNetworkFetchAllowed(trust, false));
    Boolean fetch_allowed = true;
    require_status("SecTrustGetNetworkFetchAllowed",
                   SecTrustGetNetworkFetchAllowed(trust, &fetch_allowed));
    if (fetch_allowed) {
        fprintf(stderr, "network fetching remained enabled\n");
        return 2;
    }
    printf("INPUT hostname=%s unix_time=%s fetch=0\n", argv[1], argv[2]);

    CFErrorRef error = NULL;
    bool trusted = SecTrustEvaluateWithError(trust, &error);
    char domain[128] = "none";
    long code = 0;
    if (error) {
        if (!CFStringGetCString(CFErrorGetDomain(error), domain, sizeof(domain),
                                kCFStringEncodingASCII)) {
            return 2;
        }
        code = (long)CFErrorGetCode(error);
        CFRelease(error);
    }
    printf("TRUST verdict=%d domain=%s code=%ld\n", trusted ? 1 : 0, domain, code);

    CFRelease(date);
    CFRelease(trust);
    CFRelease(policy);
    CFRelease(hostname);
    CFRelease(certificates);
    return 0;
}
