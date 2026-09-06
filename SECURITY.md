# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in MsSqlToPg, please report it responsibly to protect our users and community.

### How to Report

**DO NOT** open a public GitHub issue for security vulnerabilities.

Instead:

1. **Email Security Report**: Send details to `dipeshpansuriya@ymail.com` with subject line `[SECURITY] MsSqlToPg Vulnerability Report`
2. **Include in your report**:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if you have one)
   - Your contact information

### Response Timeline

- **Acknowledgment**: Within 24 hours
- **Initial Assessment**: Within 48 hours
- **Fix/Update**: Within 7 days (depending on severity)
- **Public Disclosure**: 30-90 days after patch release

### Severity Levels

#### 🔴 Critical (Score 9.0-10.0)
- Allows remote code execution
- Database compromise
- Complete authentication bypass
- Immediate patch required

#### 🟠 High (Score 7.0-8.9)
- Unauthorized data access
- Privilege escalation
- Denial of service
- Patch within 7 days

#### 🟡 Medium (Score 4.0-6.9)
- Partial data exposure
- Limited privilege escalation
- Patch within 30 days

#### 🟢 Low (Score 0.1-3.9)
- Minimal impact
- Requires specific conditions
- Patch within 90 days

## Security Best Practices

### For Users

1. **Keep Updated**
   - Always use the latest version
   - Subscribe to release notifications
   - Review security advisories

2. **Secure Configuration**
   ```python
   # ✅ DO: Use environment variables for secrets
   db_password = os.getenv('DB_PASSWORD')
   
   # ❌ DON'T: Hardcode secrets
   db_password = 'secret-password-12345'
   ```

3. **Database Security**
   - Use strong, unique passwords
   - Enable encryption for connections (SSL/TLS)
   - Regular backups
   - Principle of least privilege

4. **Network Security**
   - Use HTTPS only
   - Enable firewall rules
   - Use VPN for remote access
   - Restrict database access by IP

### For Contributors

1. **Code Security**
   ```python
   # ❌ SQL Injection Risk
   query = f"SELECT * FROM users WHERE email = '{email}'"
   
   # ✅ Parameterized Query
   cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
   ```

2. **Input Validation**
   ```python
   # ✅ Validate and sanitize all inputs
   if not email or not is_valid_email(email):
       raise ValueError("Invalid email")
   if not password or len(password) < 8:
       raise ValueError("Password too short")
   ```

3. **Avoid Common Vulnerabilities**
   - SQL Injection
   - Command Injection
   - Path Traversal
   - Insecure Deserialization
   - Hardcoded Secrets

4. **Secure Dependencies**
   ```bash
   # Check for vulnerable packages
   pip install safety
   safety check
   
   # Update packages
   pip install --upgrade -r requirements.txt
   ```

## Data Protection

### Encryption

```python
from cryptography.fernet import Fernet

# Generate key (store securely)
key = Fernet.generate_key()
cipher = Fernet(key)

# Encrypt sensitive data
encrypted_data = cipher.encrypt(b"sensitive_info")

# Decrypt
decrypted_data = cipher.decrypt(encrypted_data)
```

### GDPR Compliance

- Right to access
- Right to be forgotten
- Data portability
- Consent management
- Privacy by design

## Incident Response

### If a vulnerability is discovered:

1. **Immediate Actions** (< 1 hour)
   - Stop further exposure
   - Notify affected users
   - Gather evidence
   - Create incident ticket

2. **Short-term** (1-24 hours)
   - Develop fix
   - Test thoroughly
   - Deploy patch
   - Monitor for exploitation

3. **Follow-up** (1-7 days)
   - Post-incident review
   - Improve processes
   - Update documentation
   - Public disclosure (if applicable)

## Compliance

MsSqlToPg complies with:
- OWASP Top 10
- CWE/SANS Top 25
- GDPR requirements

## Contact & Attribution

- **Security Lead**: Dipesh Pansuriya
- **Email**: dipeshpansuriya@ymail.com
- **Response Time**: Best effort basis
- **Disclosure Timeline**: 90-day standard + extension if needed

## Additional Resources

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [CWE/SANS Top 25](https://cwe.mitre.org/top25/)
- [Python Security Best Practices](https://python.readthedocs.io/en/stable/library/security_warnings.html)
- [NIST Cybersecurity Framework](https://www.nist.gov/cyberframework)

---

**Last Updated**: 2026-09-06
**Next Review**: 2026-12-06
