# Security policy

## Reporting a vulnerability

Please do not include credentials, API keys, private repository links, personal data, or sensitive browser/document contents in a public issue. If GitHub Private vulnerability reporting is enabled for this repository, use its private security advisory flow. Otherwise, contact the repository maintainers through a private channel available from the repository hosting service. Do not assume a public issue is confidential.

Please include the affected release or commit, a concise impact description, and safe reproduction steps. Redact secrets and real user data. Avoid testing against accounts or systems you do not own or have permission to use.

## Scope and limitations

The desktop service uses local Windows applications and browser automation. Sign-in and CAPTCHA are manual. Supported workflows are bounded; a browser or Office action with an uncertain outcome is recorded as unknown and is not automatically replayed. These safeguards reduce some risks but do not guarantee prevention of data loss, account impact, or unintended external effects.

Keep `DEEPSEEK_API_KEY.txt`, `data/agent_service/`, task and Office run data, and any permission files local. Review task content before enabling cloud model permission because request content may be sent to the configured provider. Do not publish logs or screenshots until they have been checked for secrets and personal information.

## Supported versions

Security reports for the current public release are welcome. No response-time or support guarantee is made.