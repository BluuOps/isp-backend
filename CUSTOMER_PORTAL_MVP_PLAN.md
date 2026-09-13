# Customer Portal MVP Plan

This is a planning artifact only. Do not implement the Customer Portal until Payments and audit logs pass staging and are approved for the next phase.

## Scope

The MVP should let a subscriber securely view their own service state and request support without exposing organization-wide OSS/BSS operations.

## Proposed capabilities

1. Login
   - Customer-specific login separate from organization staff/admin login.
   - Never authenticate against PPPoE passwords directly.
   - Use a customer identity table or membership table with hashed portal password.
   - Include password reset and session expiry.

2. Dashboard
   - Current account status.
   - Online/offline indicator from active `radacct` sessions.
   - Current plan.
   - Expiration date.
   - Latest payment status.
   - Open support ticket count.

3. Current plan
   - Plan name.
   - Rate limit.
   - Renewal price.
   - Billing cycle.
   - Next expiration/renewal date.

4. Subscription status
   - Active, expired, suspended, pending, terminated.
   - Plain-language explanation of what each state means.

5. Renew subscription
   - Create a pending renewal intent.
   - Do not integrate a gateway until the payment provider phase.
   - Manual payment flow can reference `payment_transactions`.

6. Payment history
   - Read-only list from organization-scoped payment transactions filtered by customer.
   - Columns: reference, amount, status, method, paid date.

7. Profile
   - Contact information.
   - Installation address.
   - Optional customer logo/avatar later.
   - Changes should create audit events and may require staff approval.

8. Change password
   - Change portal password only.
   - Do not change PPPoE password unless a separate explicit workflow is approved.

9. Support tickets
   - Create ticket.
   - View ticket status and staff replies.
   - Attachments later.

## Required backend foundation

- Customer portal auth tables.
- Customer sessions/JWT claims.
- Customer-to-organization resolver.
- Customer-only RBAC scope.
- Customer audit events.
- Ticket tables and APIs.
- Payment history endpoint filtered by authenticated customer.

## Security rules

- Do not trust frontend tenant IDs.
- A customer token can only access its own customer record.
- Never expose PPPoE password hashes or shared secrets.
- Keep organization staff/admin APIs separate from customer APIs.
- Audit login, password reset, payment renewal intent, and ticket actions.

## Validation checklist

- Customer cannot access another customer by ID.
- Customer cannot call organization/admin APIs.
- Expired/suspended status is displayed accurately.
- Payment history is filtered to the customer.
- Support tickets are organization-scoped and customer-scoped.
- Password reset does not affect PPPoE authentication.
