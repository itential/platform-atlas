# Security Policy

## Supported Versions

<!-- MAINTAINER: Replace the example rows below with your project's actual versions and support status -->

| Version | Supported          |
| ------- | ------------------ |
| x.x.x   | :white_check_mark: |
| x.x.x   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in this project:

1. **Do not** create a public GitHub issue
2. Report via one of the following:
   - **Preferred:** [GitHub Security Advisories](https://github.com/itential/platform-atlas/security/advisories/new) (report privately)
   - **Alternative:** security@itential.com
3. Include in your report:
   - Description of the vulnerability
   - Steps to reproduce
   - Affected versions
   - Impact assessment
   - Suggested fix (if any)

We will acknowledge your report within 48 hours and provide regular updates on our progress toward a fix. We follow coordinated disclosure practices.

## Security Best Practices

<!-- MAINTAINER: Update the sections below for your project's technology stack. Remove items that don't apply and add stack-specific guidance (e.g., SQL injection prevention for database projects, CSRF protection for web apps). -->

- **Credentials:** Never hardcode secrets, API keys, or passwords. Use environment variables or a secrets manager.
- **Dependencies:** Keep dependencies up to date. Run security scans regularly and monitor advisories.
- **Input validation:** Validate and sanitize all external input at system boundaries.
- **Error handling:** Sanitize error messages before exposing them. Avoid logging sensitive data.
- **TLS:** Always use HTTPS in production environments.
- **Access control:** Follow the principle of least privilege for all credentials and permissions.

<!-- MAINTAINER: Add any project-specific security considerations below. Examples:
- Authentication/authorization requirements
- Data encryption standards
- Compliance requirements (SOC 2, GDPR, etc.)
- Security testing tools used in CI/CD
-->
