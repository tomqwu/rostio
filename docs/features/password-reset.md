# Password Reset Feature - BDD Scenarios

## Current Web Delivery Contract

Run the SignUpFlow web app at `FRONTEND_URL` (or `APP_URL` when unset); use the
public HTTPS origin in production. `POST /auth/forgot` attaches its email task
to the HTML response. Reset emails contain `/auth/reset/{token}` browser links
and retain the `signupflow:///reset-password?token=...` mobile deep link.
The API endpoints are `POST /api/v1/auth/forgot-password` and
`POST /api/v1/auth/reset-password`; older scenario endpoint names below are historical.

Keep `DEBUG_RETURN_RESET_TOKEN` off in production. Return the same generic
message for known and unknown accounts. Log failed sends without including
email addresses or reset tokens; request a fresh link to retry delivery, which
invalidates the previous token. Background tasks are best-effort, not a durable
queue. Track durable delivery and staging-provider verification in #266 and #262.

Run `poetry run pytest tests/web/test_password_reset.py tests/api/test_password_reset_email.py`
to verify captured email links, one-time use, token rotation, and send failures
without external email delivery.

## Feature Overview
Users can reset their password if they forget it by receiving a secure reset link via email. The reset token expires after 1 hour for security.

---

## Scenario 1: User Requests Password Reset

**Given**: A user has an existing account with email "user@test.com"
**When**: User clicks "Forgot Password" on login screen
**And**: User enters their email address
**And**: User clicks "Send Reset Link"
**Then**: A password reset email is sent to the user
**And**: The email contains a unique reset token
**And**: The token expires after 1 hour
**And**: User sees confirmation message "Check your email for reset instructions"

**API Endpoint**: `POST /api/auth/password-reset/request`
**Request**:
```json
{
  "email": "user@test.com"
}
```

**Response** (200 OK):
```json
{
  "message": "Password reset email sent if account exists",
  "email_sent": true
}
```

**Security Note**: Always return success message even if email doesn't exist (prevents email enumeration attacks).

---

## Scenario 2: User Resets Password with Valid Token

**Given**: User has received a password reset email with a valid token
**When**: User clicks the reset link in the email
**And**: User is redirected to password reset page with token in URL
**And**: User enters new password "NewSecure123!"
**And**: User confirms new password "NewSecure123!"
**And**: User clicks "Reset Password"
**Then**: Password is updated in the database
**And**: Reset token is invalidated (can't be reused)
**And**: User sees success message "Password reset successful"
**And**: User is redirected to login page
**And**: User can log in with new password

**API Endpoint**: `POST /api/auth/password-reset/confirm`
**Request**:
```json
{
  "token": "abc123xyz456secure",
  "new_password": "NewSecure123!"
}
```

**Response** (200 OK):
```json
{
  "message": "Password reset successful",
  "success": true
}
```

---

## Scenario 3: User Tries Invalid Reset Token

**Given**: User has a password reset link
**When**: User tries to use an invalid or non-existent token
**Then**: API returns 400 Bad Request
**And**: User sees error message "Invalid or expired reset link"
**And**: User is shown option to request a new reset link

**API Endpoint**: `POST /api/auth/password-reset/confirm`
**Request**:
```json
{
  "token": "invalid_token_12345",
  "new_password": "NewPassword123!"
}
```

**Response** (400 Bad Request):
```json
{
  "detail": "Invalid or expired reset token"
}
```

---

## Scenario 4: User Tries Expired Reset Token

**Given**: User requested password reset more than 1 hour ago
**And**: User has not yet reset their password
**When**: User clicks the old reset link
**Then**: API returns 400 Bad Request
**And**: User sees error message "This reset link has expired"
**And**: User is prompted to request a new reset link

**Expiration Logic**:
- Reset tokens expire after 1 hour (3600 seconds)
- Expired tokens are automatically cleaned up from database

---

## Scenario 5: User Tries Weak Password

**Given**: User is on password reset page with valid token
**When**: User enters a weak password "123"
**And**: User clicks "Reset Password"
**Then**: API returns 422 Unprocessable Entity
**And**: User sees error message "Password must be at least 8 characters"
**And**: User remains on reset page to try again

**Password Requirements**:
- Minimum 8 characters
- No maximum length (reasonable limit: 128 chars)
- No complexity requirements (modern best practice per NIST)

**API Response** (422):
```json
{
  "detail": [
    {
      "loc": ["body", "new_password"],
      "msg": "Password must be at least 8 characters",
      "type": "value_error"
    }
  ]
}
```

---

## Scenario 6: User Requests Multiple Resets

**Given**: User has already requested a password reset
**When**: User requests another reset before using the first token
**Then**: Previous token is invalidated
**And**: New token is generated and sent
**And**: Only the most recent token works

**Security Benefit**: Prevents token accumulation, ensures user always has fresh token.

---

## Scenario 7: User Successfully Logs In After Reset

**Given**: User has successfully reset their password to "NewPassword123!"
**When**: User navigates to login page
**And**: User enters email "user@test.com"
**And**: User enters password "NewPassword123!"
**And**: User clicks "Login"
**Then**: User is authenticated successfully
**And**: User is redirected to main application
**And**: Old password no longer works

---

## Scenario 8: Non-existent Email Request (Security)

**Given**: An attacker tries to enumerate valid emails
**When**: Attacker requests password reset for "nonexistent@test.com"
**Then**: API returns same success message as valid emails
**And**: No email is actually sent
**And**: Response time is consistent (prevents timing attacks)

**Security Note**: This prevents email enumeration attacks where attackers try to discover valid email addresses in the system.

---

## Email Template

**Subject**: Reset Your Rostio Password

**Body**:
```
Hi [Name],

You requested to reset your password for your Rostio account.

Click the link below to reset your password:
[Reset Password Button] → https://rostio.app/reset-password?token=abc123xyz456

This link expires in 1 hour for security reasons.

If you didn't request this, you can safely ignore this email.

Best regards,
The Rostio Team
```

---

## Database Schema

**Password Reset Tokens Table**:
```python
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: str  # Primary key (UUID)
    person_id: str  # Foreign key to Person
    token: str  # Unique reset token (hashed)
    created_at: datetime  # Timestamp of creation
    expires_at: datetime  # Timestamp of expiration (created_at + 1 hour)
    used: bool  # Whether token has been used
```

**Indexes**:
- Unique index on `token` for fast lookup
- Index on `person_id` for cleanup queries
- Index on `expires_at` for cleanup queries

---

## Security Considerations

1. **Token Generation**: Use `secrets.token_urlsafe(32)` for cryptographically secure tokens
2. **Token Storage**: Hash tokens before storing in database (like passwords)
3. **Rate Limiting**: Limit password reset requests to prevent spam
   - Max 3 requests per hour per IP
   - Max 3 requests per hour per email
4. **Email Enumeration Prevention**: Always return success message
5. **Token Expiration**: Strict 1-hour expiration
6. **One-time Use**: Tokens invalidated after successful reset
7. **HTTPS Required**: Reset links must use HTTPS in production
8. **No Password in URL**: Password only submitted via POST body, never in URL

---

## Rate Limiting Configuration

**Environment Variables**:
```bash
# Password Reset Rate Limit (per IP)
RATE_LIMIT_PASSWORD_RESET_MAX=3
RATE_LIMIT_PASSWORD_RESET_WINDOW=3600  # 1 hour

# Password Reset Confirm Rate Limit (per IP)
RATE_LIMIT_PASSWORD_RESET_CONFIRM_MAX=5
RATE_LIMIT_PASSWORD_RESET_CONFIRM_WINDOW=300  # 5 minutes
```

---

## API Endpoints Summary

| Endpoint | Method | Rate Limit | Description |
|----------|--------|------------|-------------|
| `/api/auth/password-reset/request` | POST | 3/hour | Request password reset email |
| `/api/auth/password-reset/confirm` | POST | 5/5min | Confirm reset with token and new password |
| `/api/auth/password-reset/verify` | GET | 10/min | Verify token is valid (for UI) |

---

## Testing Requirements

### Unit Tests
- Token generation and hashing
- Token expiration logic
- Password validation
- Email enumeration prevention
- Token invalidation after use

### Integration Tests
- Complete password reset flow (request → email → confirm)
- Token cleanup for expired tokens
- Multiple reset requests invalidate previous tokens
- Old password no longer works after reset

### E2E Tests
- User completes full password reset workflow via GUI
- User cannot reuse expired token
- User cannot reuse already-used token
- User successfully logs in with new password
- Invalid token shows appropriate error message

---

## Implementation Priority

1. Database migration for `password_reset_tokens` table
2. Token generation and validation utilities
3. API endpoint: `POST /api/auth/password-reset/request`
4. API endpoint: `POST /api/auth/password-reset/confirm`
5. API endpoint: `GET /api/auth/password-reset/verify` (optional, for UI validation)
6. Email template for password reset
7. Frontend: "Forgot Password" link on login screen
8. Frontend: Password reset request page
9. Frontend: Password reset confirmation page (with token from URL)
10. Rate limiting for password reset endpoints
11. Cleanup job for expired tokens (background task)
12. Unit tests (TDD approach)
13. Integration tests
14. E2E tests

---

## Success Criteria

- User can request password reset via email
- User receives reset email within 1 minute
- User can reset password with valid token
- Old password no longer works after reset
- Expired tokens are rejected with clear error message
- Invalid tokens are rejected with clear error message
- System prevents email enumeration attacks
- Rate limiting prevents abuse
- All tests passing (unit + integration + E2E)
