// Observe the fixture's real trust evaluation without changing its result.
#include <CommonCrypto/CommonDigest.h>
#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void record_verdict(SecTrustRef trust, const char *api, long status,
                           long verdict) {
    const char *expected = getenv("OPENAI_AGENTS_TEST_TLS_LEAF_SHA256");
    const char *path = getenv("OPENAI_AGENTS_TEST_TLS_VERDICTS");
    if (!expected || !path || SecTrustGetCertificateCount(trust) < 1) return;
    SecCertificateRef leaf = SecTrustGetCertificateAtIndex(trust, 0);
    if (!leaf) return;
    CFDataRef der = SecCertificateCopyData(leaf);
    if (!der) return;
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(CFDataGetBytePtr(der), (CC_LONG)CFDataGetLength(der), digest);
    CFRelease(der);
    char hex[CC_SHA256_DIGEST_LENGTH * 2 + 1];
    for (size_t i = 0; i < sizeof(digest); ++i) {
        snprintf(hex + 2 * i, 3, "%02x", digest[i]);
    }
    if (strcmp(hex, expected) != 0) return;
    char line[256];
    int length = snprintf(line, sizeof(line), "%s %s %ld %ld\n",
                          hex, api, status, verdict);
    int fd = open(path, O_WRONLY | O_APPEND | O_CLOEXEC | O_NOFOLLOW);
    if (fd >= 0) {
        if (length > 0 && (size_t)length < sizeof(line)) {
            (void)write(fd, line, (size_t)length);
        }
        close(fd);
    }
}

static OSStatus observed_evaluate(SecTrustRef trust, SecTrustResultType *result) {
    OSStatus status = SecTrustEvaluate(trust, result);
    int saved_errno = errno;
    record_verdict(trust, "legacy", status,
                   status == errSecSuccess && result ? (long)*result : -1);
    errno = saved_errno;
    return status;
}

static Boolean observed_evaluate_with_error(SecTrustRef trust, CFErrorRef *error) {
    Boolean accepted = SecTrustEvaluateWithError(trust, error);
    int saved_errno = errno;
    long status = 0;
    if (!accepted) {
        status = error && *error && CFEqual(CFErrorGetDomain(*error), kCFErrorDomainOSStatus)
                     ? CFErrorGetCode(*error) : 1;
    }
    record_verdict(trust, "modern", status, accepted);
    errno = saved_errno;
    return accepted;
}

// dyld excludes this image's own calls, so both wrappers call Apple's original.
#define OBSERVE(replacement, original) \
    __attribute__((used, section("__DATA,__interpose"))) \
    static const struct { const void *new_function; const void *old_function; } \
        replacement##_interpose = { (const void *)replacement, (const void *)original }

OBSERVE(observed_evaluate, SecTrustEvaluate);
OBSERVE(observed_evaluate_with_error, SecTrustEvaluateWithError);
