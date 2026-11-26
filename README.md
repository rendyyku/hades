# HADES v2 — Sinyal awal dari data publik

Sebelum sebuah token di-listing, diintegrasikan ke wallet, atau mengadakan airdrop, pihak-pihak yang terlibat harus menyiapkan infrastruktur, dan persiapan itu sering meninggalkan jejak publik. HADES memantau beberapa jejak itu, langsung mengecek apakah tokennya sudah bisa dibeli onchain dan seberapa dalam likuiditasnya, lalu mengirim alert singkat ke Telegram. Semuanya juga tampil di dashboard web.

HADES juga mencatat **kapan listing benar-benar terjadi** di exchange (ground truth). Dengan begitu kamu bisa mengukur watcher mana yang memang lebih dulu tahu dan mana yang cuma noise.

> Kode HADES v1 (smart-money Solana, Hyperliquid) disimpan di tag git `hades-solana-final`.

## Apa yang dipantau

| Watcher | Sumber | Contoh sinyal |
|---|---|---|
| `repo_diff` | File daftar token/aset di repo GitHub pilihanmu: Trust Wallet `assets`, file token Robinhood Chain milik BitGo (kustodian), token list lain | Token baru ditambahkan lewat PR (sinyal paling awal) atau commit |
| `ct` | Certificate Transparency (crt.sh) untuk domain di watchlist | Subdomain baru `claim.`, `airdrop.`, `stake.` … |
| `ground_truth` | API publik Coinbase, Kraken, OKX, Binance, Bybit | Pair baru mulai diperdagangkan (dicatat saja, tidak di-alert) |

Setiap sinyal otomatis dicek ke **GeckoTerminal** (cadangannya **DexScreener**): sudah bisa dibeli atau belum, likuiditas pool terbesar, DEX, dan link pool. Cek ulang dilakukan 24 jam kemudian. Kalau token yang tadinya belum bisa dibeli ternyata sudah bisa, kamu dapat alert "Sekarang bisa dibeli".

Chain prioritas bawaan (mendapat bonus skor): **Robinhood Chain, MegaETH, Monad, HyperEVM**. Semua bisa diubah di `config.yaml`.

## Yang kamu butuhkan

- Python 3.12 dan Node.js 20
- Token GitHub. Gratis, tanpa scope apa pun, cukup untuk repo publik. Tanpa token, batasnya 60 request per jam, dan itu terlalu sedikit.
- Bot Telegram (opsional, tapi itu inti alert-nya)

### 1. Bikin bot Telegram

1. Buka Telegram, chat **@BotFather**, kirim `/newbot`, lalu ikuti langkahnya. Kamu akan dapat token seperti `123456:ABC...`.
2. Kirim pesan apa saja ke bot barumu.
3. Buka `https://api.telegram.org/bot<TOKEN>/getUpdates` di browser. Angka di `"chat":{"id":...}` adalah **chat ID** kamu.

Bot hanya menjawab chat ID ini. Perintah dari orang lain diabaikan.

### 2. Bikin token GitHub

GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained token**. Pilih "Public repositories (read-only)", tanpa permission tambahan.

### 3. Isi konfigurasi

```bash
cp .env.example .env                  # isi GITHUB_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HADES_API_KEY
cp config.example.yaml config.yaml    # repo, chain, domain, bobot skor
cp web/.env.example web/.env.local    # HADES_API_KEY yang sama dengan .env
```

Untuk `HADES_API_KEY`, pakai string acak yang panjang:
`python -c "import secrets;print(secrets.token_urlsafe(32))"`

Rahasia hanya ada di `.env` dan `web/.env.local`, dan keduanya diabaikan git. Jangan pernah menaruh rahasia di `config.yaml`.

## Menjalankan di Windows (dev)

```powershell
cd engine
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m hades run          # engine + API di http://127.0.0.1:8000
```

Di terminal kedua:

```powershell
cd web
npm install
npm run dev                                     # dashboard di http://127.0.0.1:3000
```

Jalankan engine dari folder `engine/`, karena `.env` menunjuk ke `../config.yaml`.

**Run pertama hanya merekam kondisi awal (baseline).** Ribuan token yang sudah ada tidak akan di-alert. Sinyal baru mulai muncul setelah ada perubahan baru.

## Menjalankan di VPS (Docker)

```bash
cp .env.example .env && cp config.example.yaml config.yaml   # lalu isi
docker compose up -d
```

- Dashboard: `http://127.0.0.1:3200`. Hanya terbuka di localhost. Akses lewat SSH tunnel (`ssh -L 3200:127.0.0.1:3200 vps`) atau reverse proxy yang punya login.
- Database SQLite ada di volume `hades-data`.
- Engine tidak membuka port ke luar. Hanya kontainer web yang bisa mengaksesnya.

## Perintah sehari-hari

| Perintah | Fungsi |
|---|---|
| `python -m hades run` | Menjalankan semuanya: watcher, alert, bot, API |
| `python -m hades once <nama-watcher>` | Uji coba satu watcher tanpa menyimpan apa pun. Nama yang salah akan menampilkan daftar nama yang benar. |
| `python -m hades evaluate [--prices]` | Menulis laporan evaluasi ke `reports/` (Markdown + CSV) |

Perintah Telegram:
- `/status`: kesehatan setiap watcher, plus jumlah sinyal, alert, dan listing dalam 24 jam
- `/watchlist`, `/watchlist add <domain>`, `/watchlist rm <domain>`: domain yang dipantau watcher CT
- `/mute <project|source|chain> [jam]`, `/mute list`, `/unmute <target>`: contohnya `/mute monad 48`

Setiap pagi jam 08:00 WIB ada ringkasan harian berisi sinyal lemah dan hasil cek ulang 24 jam.

## Menambah sumber

**Repo baru.** Tambahkan target di `config.yaml` → `repo_diff.targets`:
- `dir_added`: setiap folder baru yang punya `info.json` dihitung sebagai satu token (gaya Trust Wallet).
- `json_tokenlist`: token list berformat Uniswap (`tokens[].chainId/address/symbol/name`). Isi `chain_map` untuk memetakan chainId ke nama chain di config.
- `regex_entries`: file kode apa pun. Regex harus punya grup bernama `symbol`, dan boleh juga `address`, `name`, `chain`. Lihat contoh BitGo di `config.example.yaml`.

**Domain baru untuk CT.** Isi `ct.domains` di config, atau pakai `/watchlist add` atau halaman Watchlist. Website token yang ditemukan `repo_diff` otomatis ikut ditambahkan.

## Membaca skor

`skor = dasar + chain prioritas + earliness + fusion` (maksimal 100). Alert dikirim kalau skor ≥ 40.

- **Dasar:** PR dibuka 40, commit di-merge 30, CT dengan subdomain "panas" 30, CT lain 10
- **Chain prioritas:** +15
- **Earliness:** +15 kalau token belum bisa dibeli atau likuiditasnya < $50k
- **Fusion:** +20 untuk setiap sumber lain yang mengenai proyek yang sama dalam 7 hari (maksimal +40)

Bobot-bobot ini **masih tebakan awal**. Setel ulang setelah kamu punya data evaluasi.

## Evaluasi: baca sebelum percaya

Halaman **Eval** (atau `hades evaluate`) menunjukkan, untuk setiap watcher:
- berapa sinyal yang diikuti listing nyata (hit)
- berapa jam lebih awal (median dan minimum)
- berapa yang tidak pernah terjadi (false positive)

Pencocokan dengan listing memakai **simbol**, jadi bisa saja keliru. Selama hit-nya masih di bawah 10, laporannya ditandai "sampel kecil, belum bisa disimpulkan".

## Batasan

- **Hanya data publik**, lewat endpoint yang terdokumentasi, dengan rate limit yang dihormati. Tidak ada peretasan, menebak URL privat, atau melewati login.
- **Tidak ada auto-buy, private key, atau signing.** HADES hanya memberi tahu. Keputusan beli tetap di tanganmu.
- **Alert bukan nasihat keuangan.** Registry seperti Trust Wallet juga menerima token sampah. Contohnya, saat verifikasi ada token bernama "FAKE ETH" di Robinhood Chain. Selalu cek bukti dan likuiditasnya sendiri.
- Kisah sukses seperti "Cash Cat" adalah survivorship bias: kita hanya mendengar yang berhasil. Percayai watcher hanya setelah evaluasinya mendukung.

## Pengembangan

```bash
cd engine && .venv/Scripts/python.exe -m pytest -q     # engine (tanpa jaringan live)
cd web && npm test && npm run build                     # web
```

Spec: `docs/superpowers/specs/2026-11-27-hades-early-signals-design.md` · Rencana: `docs/superpowers/plans/2026-11-27-hades-v2-early-signals.md` · Verifikasi data awal: `docs/superpowers/notes/2026-11-27-seed-verification.md`
