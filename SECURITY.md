# Security

## Reporting

Please report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab), not in a public
issue. You will get a response within 72 hours.

## Model

* **No secrets in the repository.** CI runs gitleaks on the full history and a guard that rejects private settings,
  keys, databases and data exports. Secrets exist only on the server as mode-600 files, entered by the owner.
* **Exchange keys cannot withdraw.** The execution code reads the key's permissions and refuses to run with a key
  that can withdraw; keys are IP-restricted to the server.
* **Pull-based deployment.** GitHub holds no credential for the server. The server deploys a commit only after all
  checks pass on GitHub and the test suite passes on the server.
* **Least privilege on the server.** Engine services run as `bees` with `NoNewPrivileges` and a read-only file
  system outside the engine directory. The operations key belongs to `opsbot`, restricted to a fixed command list
  (forced command; no shell, no forwarding). SSH: keys only, no root, fail2ban, firewall allows SSH only.
* **Website.** Strict Content-Security-Policy (inline script pinned by hash), the chart library pinned with
  Subresource Integrity, no cookies, no third-party trackers, `noindex`.
* **Code scanning.** CodeQL (Python, JavaScript) on every push and weekly; Dependabot for workflow actions.
