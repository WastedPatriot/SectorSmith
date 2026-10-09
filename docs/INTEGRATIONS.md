# Integrations

SectorSmith's integrations are optional and read-mostly. Each one stays off until it is set up in
Settings > Integrations, and none of them stores a password or API key in plain text.

## Built (1.4.x)

### ScreenConnect (ConnectWise Control)
- Settings: the instance address (https only, checked and normalized), plus an optional instance for the
  current client. Stored in `settings.json` as `screenconnect_url` and `screenconnect_clients`.
- Machines page and Ctrl+K: "Connect with ScreenConnect" opens the host page in the default browser, searching
  the "All Machines" group for the machine name (`/Host#Access/All%20Machines/<name>`, every part percent-encoded).
  The technician signs in to ScreenConnect as usual. No API is called, so nothing secret is needed.
- If this PC has the ScreenConnect client installed, its relay host, session ID and custom properties
  (Company, Site...) are read from the client service's command line in the registry, read-only.
- Code: `sectorsmith/integrations/screenconnect.py`, UI glue in `sectorsmith/ui/integrations_ui.py`.

### Active Directory lookup
- Only on a domain-joined PC, only against that PC's own domain, as the signed-in technician. No credentials.
- ADSI through Windows PowerShell (`[adsisearcher]`). The script is fixed text sent with `-EncodedCommand`; the
  LDAP filter is built in Python with RFC 4515 escaping and passed in an environment variable.
- Returns display name, email, department, enabled, last logon (`lastLogonTimestamp`, can lag up to 14 days),
  group names, home drive and folder, and logon script. Statuses: ok, not_found, not_joined, unavailable, error.
- Migrate user's "From" step shows it as a card under the profile list. The wizard keeps the result in
  `MigrateWizard.domain_user`, and `ADUser.migration_notes()` gives the home drive and logon script lines a
  migration report can include.
- Runs in the background with a 25 s timeout and stops when the step is left or another user is picked.
- Code: `sectorsmith/integrations/ad.py`.

### Secrets
- `sectorsmith/integrations/dpapi.py` encrypts secrets for the current Windows user with DPAPI
  (`CryptProtectData`, via ctypes) into `secrets.json`. On other systems saving a secret is refused.
  Nothing uses it yet; it is there for the first integration that needs an API key.

## Not built: Microsoft 365

What a Microsoft 365 lookup (user, licences, OneDrive) would need, so the owner can decide whether it is worth it.

**Entra app registration (one per MSP, multi-tenant)**
- Register one public client app in the MSP's own Entra tenant, "Accounts in any organizational directory".
- Platform: Mobile and desktop, redirect `https://login.microsoftonline.com/common/oauth2/nativeclient`.
  "Allow public client flows" on. No client secret or certificate: it is a desktop app.
- Each customer tenant consents once (an admin signs in and accepts), which creates the enterprise app there.
  Customers with GDAP through Partner Center could use that instead, but it is more set-up.

**Sign-in: delegated device-code flow**
- The technician gets a code, signs in at microsoft.com/devicelogin as a customer admin (or their GDAP
  account), and SectorSmith receives a delegated token. The token acts as that person, so it can't do more
  than they can.
- Keep the access token in memory only. If a refresh token is kept, store it with `dpapi.save_secret` per
  tenant and give a "Sign out" that forgets it.
- Implementing it needs either MSAL for Python (a new dependency, MIT licensed) or about 150 lines of plain
  HTTPS against `/oauth2/v2.0/devicecode` and `/oauth2/v2.0/token`. HTTPS from the standard library works.

**Graph scopes (delegated, least first)**
- `User.Read.All`: profile, UPN, department, account enabled. Last sign-in (`signInActivity`) also needs
  `AuditLog.Read.All`.
- `Directory.Read.All` or `LicenseAssignment.Read.All` (newer): `/users/{id}/licenseDetails` for assigned SKUs.
- `Files.Read.All`: `/users/{id}/drive` for OneDrive quota and used space, and whether the drive exists yet.
- `GroupMember.Read.All` for group names, if wanted.
- Most of these need admin consent in each customer tenant.

**Where it would show**
- Migrate user "From" step, next to the domain card: licence names, whether OneDrive is provisioned and how
  full it is, which decides whether OneDrive folders need copying or will sync.
- Settings > Integrations: tenant list, consent link per tenant, signed-in account, sign out.

**Risks**
- A stolen refresh token is a delegated admin session; keep it DPAPI-protected and short-lived, or don't keep it.
- Consent per customer tenant is the main friction. Without it nothing works.
- Graph throttling is not a concern at bench scale.
