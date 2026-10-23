# Hosting on Fly.io as a Claude connector (read-only)

This fork adds a hosted mode for using the server as a Claude custom connector, including from the Claude mobile app.

When `MCP_SECRET` is set, the server:

- listens publicly on `PORT` (8080 on Fly);
- only answers at `/<MCP_SECRET>/mcp`, so the secret in the URL works as the password;
- is **always read-only**: the 10 tools that place or cancel orders, change pies, or request exports are removed, and the API client refuses any non-GET request as a second guard;
- serves `/health` for uptime checks.

Local stdio use works exactly as before.

## Steps

1. **Trading 212 key.** In the Trading 212 app, go to Settings → API. Create a key with read permissions only (account, portfolio, history, metadata, pies read). Copy the key and the secret.
2. **Secret.** Make a random string of 32+ letters and digits for `MCP_SECRET`.
3. **Fly.** Choose "Launch an App from GitHub" and pick this repo. It uses `fly.toml` and the `Dockerfile`.
4. **Secrets in Fly.** Add the following in the launch screen or in the app's Secrets page:

   | Secret | Value |
   |---|---|
   | `TRADING212_API_KEY` | your key |
   | `TRADING212_API_SECRET` | your secret |
   | `ENVIRONMENT` | `live` (or `demo` for a Practice-account key) |
   | `MCP_SECRET` | your random string |

5. **Check.** Opening `https://<app-name>.fly.dev/health` should show `{"ok": true, ...}`.
6. **Claude.** At claude.ai, go to Settings → Connectors → Add custom connector and paste `https://<app-name>.fly.dev/<MCP_SECRET>/mcp`. Then turn the connector on in a chat on any device.

Anyone with the full URL can read the account. To revoke access, change `MCP_SECRET` in Fly and update the connector.
