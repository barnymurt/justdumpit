# DNS Setup for justdumpit.online

After you buy the domain, point it at your Oracle Cloud VM's public IP.

## Get your VM's public IP

In Oracle Cloud Console:
- Compute → Instances → click your instance
- Copy the **Public IP** (something like `123.45.67.89`)

## Add these records at your registrar

Wherever you bought the domain (Cloudflare, Namecheap, Porkbun, etc.):

| Type | Host | Value | TTL |
|------|------|-------|-----|
| A | `@` | `<your-oracle-public-ip>` | Auto or 300 |
| A | `www` | `<your-oracle-public-ip>` | Auto or 300 |
| AAAA | `@` | `<your-oracle-public-ipv6>` (optional) | Auto |
| AAAA | `www` | `<your-oracle-public-ipv6>` (optional) | Auto |

The `@` record is the apex (`justdumpit.online`).
The `www` record makes `www.justdumpit.online` work too — Caddy will redirect to the apex.

## Verify DNS is propagating

```
nslookup justdumpit.online
dig justdumpit.online +short
```

Both should return your Oracle IP within a few minutes (sometimes up to 48h for full global propagation, but usually < 5 min).

## Then run the setup script with your domain

```
export YTSCRAPER_DOMAIN=justdumpit.online
export MINIMAX_API_KEY='your-key'
sudo -E bash oracle-setup.sh
```

Caddy will auto-request a Let's Encrypt cert on first request. First request may take ~30 seconds while the cert is issued; subsequent requests are fast.

## If you don't want to buy a domain yet

Skip the `YTSCRAPER_DOMAIN` env var. Caddy will serve on plain HTTP port 80 with the raw IP. Works fine for personal use, just no HTTPS.