# Auth Review Queue Routing

- Route to auth-review-queue when prior authorization is required, likely required, or blocked by
  missing CPT/diagnosis details.
- Route to care-team-intake-queue when referral or scheduling data is incomplete but payer auth is
  not yet indicated.
- Route to billing-review-queue only for claim denial, refund, or balance disputes.
- High-priority auth review applies when surgery or advanced imaging is expected within 14 days.
