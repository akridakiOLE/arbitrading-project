# DEPLOYMENT — Arbitrading Bot στο Hetzner CX23

Οδηγός για να στήσεις τον bot + web dashboard στον ίδιο Hetzner server που τρέχει το FastWrite.

---

## 0. Overview

- **Backend:** Flask + Gunicorn (port 5001, 127.0.0.1 only)
- **Frontend:** Nginx reverse proxy + TLS (subdomain ή subpath)
- **Process:** Single systemd unit `arbitrading.service`
- **Data:** SQLite files στο `/opt/arbitrading-project/` (δεν φεύγει τίποτα από το server)
- **Συνύπαρξη με FastWrite:** Διαφορετική port (5001 vs FastWrite port), διαφορετικό systemd unit, διαφορετικό subdomain/subpath

---

## 1. Push local code → GitHub

Στο Windows:

```powershell
cd C:\Users\User\Desktop\arbitrading-project
git add .
git commit -m "v4 Phase 3a/3b/3c - paper + live + web UI"
git push origin master
```

---

## 2. SSH στο Hetzner server

```bash
ssh root@<hetzner-ip>
```

## 3. Δημιουργία user για το bot (μία φορά)

```bash
sudo adduser --system --group --home /opt/arbitrading-project arbitrading
sudo mkdir -p /var/log/arbitrading
sudo chown arbitrading:arbitrading /var/log/arbitrading
```

## 4. Clone ή Pull repo

**Πρώτη φορά:**
```bash
cd /opt
sudo -u arbitrading git clone https://github.com/akridakiOLE/arbitrading-project.git
cd arbitrading-project
```

**Ενημέρωση:**
```bash
cd /opt/arbitrading-project
sudo -u arbitrading git pull
```

## 5. Python venv + dependencies

```bash
cd /opt/arbitrading-project
sudo -u arbitrading python3 -m venv .venv
sudo -u arbitrading .venv/bin/pip install --upgrade pip
sudo -u arbitrading .venv/bin/pip install -r requirements.txt
sudo -u arbitrading .venv/bin/pip install flask gunicorn
```

## 6. Secrets

```bash
sudo -u arbitrading cp config/secrets.env.example config/secrets.env
sudo -u arbitrading nano config/secrets.env
```

Συμπλήρωσε:
```
KUCOIN_API_KEY=...
KUCOIN_API_SECRET=...
KUCOIN_API_PASSPHRASE=...
FLASK_SECRET_KEY=<random long string, π.χ. python -c "import secrets; print(secrets.token_hex(32))">
ARBITRADING_WEB_PASSWORD=<το password σου για login στο UI>
```

```bash
sudo chmod 600 config/secrets.env
```

## 7. systemd unit

```bash
sudo cp deployment/arbitrading.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable arbitrading
sudo systemctl start arbitrading
sudo systemctl status arbitrading
```

Logs:
```bash
sudo journalctl -u arbitrading -f
sudo tail -f /var/log/arbitrading/error.log
sudo tail -f /var/log/arbitrading/stdout.log
```

## 8. DuckDNS subdomain

Στο [duckdns.org](https://duckdns.org), δημιούργησε `arbitrading.duckdns.org` να δείχνει στην IP του Hetzner.

## 9. Nginx + TLS

```bash
sudo cp deployment/nginx-arbitrading.conf /etc/nginx/sites-available/arbitrading
sudo ln -s /etc/nginx/sites-available/arbitrading /etc/nginx/sites-enabled/
sudo nginx -t
```

### TLS με certbot:
```bash
sudo certbot --nginx -d arbitrading.duckdns.org
```

Αν όλα καλά:
```bash
sudo systemctl reload nginx
```

---

## 10. Πρόσβαση στο UI

Άνοιξε στον browser σου:

```
https://arbitrading.duckdns.org
```

Login με το `ARBITRADING_WEB_PASSWORD` που έβαλες στο `.env`.

---

## 11. Workflow από UI

### Πρώτη φορά — Paper mode πρώτα:
1. Πάτα "Stop" (αν είναι running)
2. Configuration:
   - mode = `paper`
   - symbol = `PEPE/USDT`
   - start_base_coin = `26000000` (~100 USDT @ 3.8e-6)
   - scale_base_coin = `4`
   - min_profit_percent = `10`
   - promote = `1`
   - second_profit_enabled = `false`
3. Πάτα "Start"
4. Παρακολούθησε το status panel να δείχνει `state=monitoring`, `cycles` να μετρά αν πετύχει trigger

### Αφού επιβεβαιώσεις ότι δουλεύει — Live mode:
1. Stop
2. Άλλαξε mode σε `live`
3. Start (θα σε ρωτήσει confirmation στο UI)

### Για Promote 3 (emergency reset):
- Πάτα το κουμπί "Resset_Invest". Θα κλείσει τις θέσεις BASE_COIN, τα VIP μένουν άθικτα, ο bot → STOPPED.

---

## 12. Updates

Όταν αλλάξει ο κώδικας:

```bash
cd /opt/arbitrading-project
sudo -u arbitrading git pull
sudo -u arbitrading .venv/bin/pip install -r requirements.txt
sudo systemctl restart arbitrading
```

---

## 13. Monitoring

### Live metrics μέσω UI
Το dashboard auto-refreshes κάθε 3 δευτερόλεπτα.

### SSH tail logs
```bash
sudo tail -f /var/log/arbitrading/stdout.log
```

### SQLite queries
```bash
cd /opt/arbitrading-project
sqlite3 live_trades.db "SELECT ts_iso, action, quantity, price FROM live_trades ORDER BY id DESC LIMIT 10"
```

---

## 14. Troubleshooting

| Symptom | Fix |
|---|---|
| UI δεν ανοίγει | `systemctl status arbitrading`, έλεγξε logs |
| "502 Bad Gateway" στο nginx | Gunicorn δεν τρέχει, restart service |
| "KuCoin API keys not found" | Τσέκαρε `config/secrets.env`, restart service |
| Process δεν σταματά | `sudo systemctl stop arbitrading`, αν χρειαστεί `pkill -9 -f gunicorn` |
| Bot κολλάει σε error | Stop από UI, read error μήνυμα, fix config, restart |

---

## 15. Σημαντικές σημειώσεις ασφάλειας

1. **API keys:** ΜΟΝΟ σε `/opt/arbitrading-project/config/secrets.env`, chmod 600, owned by `arbitrading` user
2. **Firewall:** Μόνο 80/443 εκτός, 5001 εσωτερικά (127.0.0.1)
3. **IP allowlist στο KuCoin API:** Βάλε την IP του Hetzner (και τη δική σου για testing)
4. **Rate limit login:** Ενεργό στο nginx config (5 requests/min)
5. **HTTPS μόνο:** Redirect HTTP → HTTPS στο nginx
6. **Web password:** Αλλαξε το default `changeme`!

---

## 16. Συνύπαρξη με FastWrite

- FastWrite: port 5000, subdomain `fastwrite.duckdns.org`
- Arbitrading: port 5001, subdomain `arbitrading.duckdns.org`
- Ξεχωριστά systemd units, ξεχωριστά nginx configs, ξεχωριστές SQLite DBs
- Τίποτα κοινό εκτός του Hetzner CX23 host

Κανονικά δεν υπάρχει σύγκρουση — απλά τρέχουν και τα δύο.
