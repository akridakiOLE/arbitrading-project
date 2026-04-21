# RUNBOOK — Live Trading Deployment (v4 Φάση 3γ)

Βήματα για εκκίνηση του arbitrading bot σε live mode στο KuCoin.

**ΣΗΜΑΝΤΙΚΟ:** Αυτός ο κώδικας στέλνει πραγματικές εντολές με πραγματικά λεφτά. Διάβασε ΟΛΟΚΛΗΡΟ τον runbook πριν τρέξεις live.

---

## 0. Προαπαιτούμενα

- Python 3.10+ εγκατεστημένο στο Windows
- Git repo cloned στο `C:\Users\User\Desktop\arbitrading-project`
- `pip install -r requirements.txt` ολοκληρωμένο
- KuCoin account με margin enabled (Cross Margin ενεργοποιημένο)

---

## 1. API Keys στο KuCoin

1. Πήγαινε: https://www.kucoin.com/account/api
2. Create API → Select:
   - **Name:** `arbitrading-bot`
   - **Permissions:** `General` + `Spot Trade` + `Margin Trade` (τσέκαρε αυτά τα 3)
   - **⚠ ΠΟΤΕ ΜΗΝ τσεκάρεις:** `Transfer`, `Withdraw`
   - **IP Restriction:** προτείνεται να βάλεις μόνο τη δημόσια IP του PC σου (whatismyip.com)
3. Save API Key, Secret, Passphrase — θα τα ξαναδείς μία μόνο φορά.

---

## 2. Configuration στο PC

```powershell
cd C:\Users\User\Desktop\arbitrading-project
copy config\secrets.env.example config\secrets.env
notepad config\secrets.env
```

Συμπλήρωσε:

```
KUCOIN_API_KEY=xxxxxxxxxxxxx
KUCOIN_API_SECRET=xxxxxxxxxxx
KUCOIN_API_PASSPHRASE=xxxxxxxxxxx
```

**Αποθήκευσε. ΜΗΝ το ανεβάσεις στο GitHub** (είναι ήδη στο .gitignore, αλλά τσέκαρε).

---

## 3. Επιβεβαίωση KuCoin Cross Margin Setup

1. Στο KuCoin UI → Margin Trading → Cross Margin (ΟΧΙ Isolated)
2. Βεβαιώσου ότι το PEPE/USDT είναι διαθέσιμο
3. Μετάφερε USDT από Main Account → Cross Margin Account (όσα χρειάζεται για start_base_coin × price)
4. Για `start_base_coin = 1_000_000_000 PEPE` με PEPE price ~3.8e-6, χρειάζεται:
   - Collateral: ~3,800 USDT σε PEPE ή USDT
   - Αρκεί π.χ. 1 δισ. PEPE ≈ 3,800 USDT value

**ΠΡΟΣΟΧΗ:** Αν το start_base_coin είναι διαφορετικό από τα PEPE που έχεις ήδη στο margin account, ο κώδικας ΔΕΝ θα διορθώσει την ασυμφωνία. Θα δουλέψει με ό,τι υπάρχει ή θα κολλήσει σε insufficient funds.

---

## 4. Προ-live έλεγχος με paper mode (30 λεπτά)

```powershell
cd C:\Users\User\Desktop\arbitrading-project
python -m core.trader_loop ^
  --symbol PEPE/USDT ^
  --start-base 1000000000 ^
  --scale 4 ^
  --min-profit 10 ^
  --promote 1 ^
  --mode paper ^
  --snapshot-every 30 ^
  --db-path paper_pre_live.db ^
  --state-db-path paper_pre_live_state.db ^
  --duration 1800
```

Ψάχνεις στα logs:
- `=== SETUP ===` και όλα τα βήματα 1-6 σωστά
- `state=monitoring` στα snapshots
- `ratio > 1.07` πάντα
- Καμία `WARNING` ή `ERROR`

---

## 5. Live Run — μικρό κεφάλαιο

**ΠΡΟΤΕΙΝΟΜΕΝΟ:** Ξεκίνα με πολύ μικρό start_base για τον πρώτο live κύκλο. Αν το PEPE price = 3.8e-6 και θες Grand_amount ~ 500 USDT:
- Κεφάλαιο: 100 USDT → 26M PEPE
- Με SCALE=4 → BUY = 104M PEPE
- Δάνειο USDT: ~400
- Grand ~ 500

```powershell
python -m core.trader_loop ^
  --symbol PEPE/USDT ^
  --start-base 26000000 ^
  --scale 4 ^
  --min-profit 10 ^
  --promote 1 ^
  --mode live ^
  --confirm-live ^
  --snapshot-every 30 ^
  --db-path live_trades.db ^
  --state-db-path live_state.db
```

Θα δεις:
- Ρητή προειδοποίηση και prompt για ENTER
- `LIVE MODE — real orders will be sent to KuCoin`
- Real balance sync από exchange
- Πρώτη εντολή BORROW_USDT προς KuCoin API

Αν βλέπεις τα πάντα OK μετά τον πρώτο SETUP (περίπου 30 δευτερόλεπτα), μπορείς να αφήσεις να τρέχει.

---

## 6. Monitoring (ενόσω τρέχει)

**Σε άλλο terminal:**

```powershell
# Τελευταία 10 trades
cd C:\Users\User\Desktop\arbitrading-project
python -c "import sqlite3; c=sqlite3.connect('live_trades.db'); [print(r) for r in c.execute('SELECT ts_iso, action, quantity, price, note FROM live_trades ORDER BY id DESC LIMIT 10')]"

# Τελευταίο state snapshot
python -c "import sqlite3, json; c=sqlite3.connect('live_state.db'); row=c.execute('SELECT ts_iso, event, state FROM state_snapshots ORDER BY id DESC LIMIT 1').fetchone(); print(row)"
```

---

## 7. Graceful Stop

**Ctrl+C στο terminal** όπου τρέχει. Ο bot:
1. Σταματάει το price feed
2. Τυπώνει final report
3. Αποθηκεύει τελικό state snapshot
4. Κλείνει SQLite connections

**ΠΡΟΣΟΧΗ:** Αν σταματήσεις mid-cycle (π.χ. έχει ανοιχτή θέση), οι θέσεις μένουν στο KuCoin. Ο bot ΔΕΝ κάνει unwind μόνος του.

Για καθαρό reset σε αρχική κατάσταση χρησιμοποίησε Promote 3 (αν το έχεις εξοπλίσει με UI) ή χειροκίνητα από το KuCoin UI.

---

## 8. Resume after Crash

Αν το process πέσει (blue screen, reboot, κτλ):

```powershell
python -m core.trader_loop ^
  --symbol PEPE/USDT ^
  --start-base 26000000 --scale 4 --min-profit 10 --promote 1 ^
  --mode live --confirm-live ^
  --resume-state ^
  --state-db-path live_state.db ^
  --db-path live_trades.db
```

Το `--resume-state` επαναφέρει την BotMemory από την τελευταία εγγραφή στο state DB. **ΠΡΟΣΟΧΗ:** πρέπει να τσεκάρεις ότι το KuCoin account είναι όντως στην κατάσταση που νομίζει ο bot (π.χ. τα borrows και oi open positions ταιριάζουν).

---

## 9. Κόκκινες γραμμές — πότε να σταματήσεις ΑΜΕΣΑ

- Τρέχει και εμφανίζονται επαναλαμβανόμενα `ERROR` ή `Traceback` → Ctrl+C ΑΜΕΣΑ
- Margin ratio πέφτει κάτω από 1.10 (είμαστε ήδη πολύ κοντά στο 1.07 trigger) → Ctrl+C, έλεγξε manually
- Βλέπεις orders που δεν αντιστοιχούν στη στρατηγική → Ctrl+C, έλεγξε το KuCoin UI

---

## 10. Γνωστοί περιορισμοί v4 Φάσης 3γ

- **Δεν υπάρχει WebSocket** — 1s polling από REST. Για volatile spikes μπορεί να χάσει ticks.
- **Δεν υπάρχει automatic reconnect σε KuCoin API failures** — μια μεγάλη outage θα σταματήσει τον bot.
- **Δεν υπάρχουν alerts/notifications** — ο bot γράφει μόνο στο terminal log.
- **Market orders μόνο** — όχι limit orders (πιο γρήγορο fill, αλλά slippage).
- **Δεν έχει τεστθεί πραγματικά σε live** — είσαι ο πρώτος δοκιμαστής. Ξεκίνα με μικρό κεφάλαιο.
- **Promote 2 (VIP) ΔΕΝ έχει ελεγχθεί σε live** — μείνε σε `--promote 1` για τον πρώτο κύκλο.

---

## Troubleshooting

| Error | Αιτία | Διόρθωση |
|---|---|---|
| `KuCoin API keys NOT found` | secrets.env λείπει/λάθος | βλ. §2 |
| `ccxt.AuthenticationError` | λάθος passphrase | δημιούργησε νέο API key |
| `ccxt.InsufficientFunds` | δεν υπάρχει αρκετό collateral στο margin account | transfer USDT/PEPE στο Cross Margin |
| `Order didn't fill within 30s` | liquidity problem στο PEPE/USDT | αυξάνουμε timeout ή δοκιμάζουμε ξανά |
| `Network error` | KuCoin API δεν απαντά | περιμένεις, ξαναπροσπαθείς |

---

**Τελευταίο:** Αν σκάσει κάτι που δεν περιμένεις, μην κάνεις panic. Πάτα Ctrl+C, γράψε ακριβώς τι είδες, μπες στο KuCoin UI να δεις την κατάσταση, και μίλα μου. Σχεδόν τα πάντα διορθώνονται με χειροκίνητη παρέμβαση στο KuCoin UI και reset του bot από fresh state.
