# Mini App setup

The optional Mini App runs in the bot process and serves its UI and API on loopback.
Commands remain available without it. Use Python and the dependencies from the main README.

1. Set `WEBAPP_PORT=8095` and `WEBAPP_URL=https://buys.example.com` in `.env`.
2. Configure your HTTPS reverse proxy to forward that hostname to `127.0.0.1:8095`.
   Keep the API port private. With Caddy, a minimal site is:

   ```caddyfile
   buys.example.com {
       reverse_proxy 127.0.0.1:8095
   }
   ```

3. Restart the bot. It sets the private-chat menu button to the Mini App URL.
4. Open it from the bot in Telegram. Opening the page directly in a browser cannot
   authenticate API requests.

Every API request carries signed Telegram `initData`; the backend checks its signature
and age. Users see chats they added; writes require current administrator rights.
Owners configured in `OWNER_IDS` can access the owner panel.

The Mini App follows the configured RPC network. Vibe/vibe promo generation and holder
milestones currently require Robinhood Chain testnet (46630). See the README feature matrix.

Both `WEBAPP_URL` and `WEBAPP_PORT` must be set to enable the server. Only an HTTPS
URL with no path, query or fragment is supported. The proxy must preserve `/api/` paths.
