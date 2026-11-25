# Security

## Reporting a problem

Please report it privately, not in a public issue.

Use **Report a vulnerability** on this repository's Security tab. If that button is not there,
open an issue that says only that you have a security report and how to reach you, with no
details, and you will be contacted.

Say what you found, which part and version, and how to reproduce it. Fixes go to the latest
version on `main`; older versions are not patched.

## What the suite does and does not protect

Knowing where the lines are is part of using it safely.

**Viewing is open.** Anyone who can reach a module's page or the suite bar can read everything
on it: names, addresses, findings, and for the Kubernetes module, if you turn it on, the last
log lines of failing containers. There is no sign-in for viewing. Run it on a network you trust
or behind a proxy that authenticates.

**Changing settings needs two things.** The Settings area is behind the suite's admin password,
and a module accepts settings only from a caller that presents the settings token it shares with
the suite. A module does not reveal its current settings without that token. Five wrong
passwords from one address lock sign-in for five minutes.

**Traffic is plain HTTP unless you add TLS.** That includes the admin password at sign-in and
anything typed into Settings. Put the suite and its modules behind TLS if the network between
you and them is not one you trust.

**Secrets at rest.** Credentials saved from Settings are kept by the module they belong to, in a
file only that module's user can read, on the module's data volume. They are not encrypted.
Passwords are never sent back to a browser, and a saved password is discarded if the address or
user it belongs to is changed. The suite bar keeps none of a module's credentials.

**Modules read; they do not change what they watch.** Each needs only a read-only account. The
one exception is the Network module's block list, which is off unless a password is set, and
works by the router fetching and re-validating the list; the module has no way to change the
router itself.

**The Kubernetes node agent is privileged.** It runs on every node with host process access to
read connection tables. It is optional: without it the module shows the cluster's objects but
not live connections.

**Certificates are not checked by default** when a module talks to a router, a UniFi controller
or an ESXi host, because those ship self-signed certificates. Each has a setting to turn
checking on.
