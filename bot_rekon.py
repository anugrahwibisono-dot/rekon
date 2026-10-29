import asyncio
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

# ==========================================
# KONFIGURASI DARI SECRETS GITHUB
# ==========================================



API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TG_SESSION_STRING = os.environ["TG_SESSION_STRING"]
WEB_APP_URL = os.environ["WEB_APP_URL"]

TARGET_GROUP_SUMBER = "GUDANG SO BLIMBING NEW"  
TARGET_GROUP_LAPORAN = "TEST BOT GUDANG BLIMBING"
TANGGAL_MULAI = 16  # Data diambil dari tanggal ini SAMPAI HARI INI

BATCH_SIZE = 50
TIMEOUT_REQUEST = 60
MAKS_PERCOBAAN = 3

WIB = timezone(timedelta(hours=7))
client = TelegramClient(StringSession(TG_SESSION_STRING), API_ID, API_HASH)

POLA_PDF = re.compile(r'\.pdf$', re.IGNORECASE)
# Awalan SC: AO + 1 huruf (AOi, AOo, AOk, AOs, ...) atau PDA + 1 huruf opsional (PDA, PDAk, ...).
# Bagian di depan tanda "-" (mis. DGPS...-AOi...) diabaikan; yang diambil setelah tanda "-".
POLA_AO = re.compile(r'(?:[A-Za-z0-9_-]+-)?((?:AO[A-Za-z]|PDA[A-Za-z]?)\d[A-Za-z0-9_-]{14,})', re.IGNORECASE)
POLA_SC_ANGKA = re.compile(r'(?<![A-Za-z0-9])(SC\d{8,15})(?!\d)', re.IGNORECASE)
EMOJI_NEGATIF = {"👎", "❌", "✖", "✖️"}


# ==========================================
# FUNGSI BANTU
# ==========================================
def hitung_rentang_tanggal():
    """(tanggal_mulai, hari_ini). Jika hari ini < TANGGAL_MULAI, mundur ke bulan sebelumnya."""
    hari_ini = datetime.now(WIB).date()
    if hari_ini.day >= TANGGAL_MULAI:
        mulai = hari_ini.replace(day=TANGGAL_MULAI)
    else:
        akhir_bulan_lalu = hari_ini.replace(day=1) - timedelta(days=1)
        mulai = akhir_bulan_lalu.replace(day=min(TANGGAL_MULAI, akhir_bulan_lalu.day))
    return mulai, hari_ini


def is_valid_sc(sc):
    sc = sc.strip()
    if len(sc) < 18 or len(sc) > 35:
        return False
    if sc.lower().count('ao') > 2 or re.search(r'(.)\1{5,}', sc):
        return False
    return True


def ekstrak_sc(teks):
    hasil = []
    for raw in POLA_AO.findall(teks):
        for sub in re.split(r'(?=(?:AO[A-Za-z]|PDA[A-Za-z]?)\d)', raw, flags=re.IGNORECASE):
            sub = POLA_PDF.sub('', sub.strip())
            if sub and is_valid_sc(sub):
                hasil.append(sub)
    for m in POLA_SC_ANGKA.findall(teks):
        hasil.append(m.upper())
    return hasil


def ada_reaksi_valid(message):
    """True bila pesan punya reaksi apa pun (mis. ✍️) selain reaksi negatif."""
    try:
        if not (message.reactions and message.reactions.results):
            return False
        for r in message.reactions.results:
            emot = getattr(r.reaction, "emoticon", None)
            if emot is None or emot not in EMOJI_NEGATIF:
                return True
    except Exception:
        pass
    return False


def kumpulkan_teks(message):
    teks = []
    if message.text:
        teks.append(message.text)
    if getattr(message, 'caption', None):
        teks.append(message.caption)
    try:
        if message.file and message.file.name:
            teks.append(message.file.name)
    except Exception:
        pass
    return teks


# ==========================================
# PROSES UTAMA
# ==========================================
async def main():
    ada_kegagalan = False

    print("🚀 Menghubungkan ke Telegram...")
    dialogs = await client.get_dialogs()

    kandidat_sumber = []
    kandidat_laporan = []
    for dialog in dialogs:
        nama = (dialog.name or "").strip().lower()
        if TARGET_GROUP_SUMBER.lower() in nama:
            kandidat_sumber.append(dialog.input_entity)
            print(f"   📌 Grup sumber cocok: '{dialog.name}', id={dialog.id}")
        if TARGET_GROUP_LAPORAN.lower() in nama:
            kandidat_laporan.append(dialog)

    # Utamakan nama yang persis sama dengan target, baru yang hanya mengandung nama tersebut
    kandidat_laporan.sort(key=lambda d: (d.name or "").strip().lower() != TARGET_GROUP_LAPORAN.lower())
    for d in kandidat_laporan:
        e = d.entity
        print(f"   🔎 Kandidat grup laporan: nama='{d.name}', id={d.id}, tipe={type(e).__name__}, "
              f"deactivated={getattr(e, 'deactivated', None)}, migrated_to={getattr(e, 'migrated_to', None)}, "
              f"megagroup={getattr(e, 'megagroup', None)}, broadcast={getattr(e, 'broadcast', None)}")

    async def kirim_laporan(teks):
        """Mencoba semua kandidat grup laporan (entity lalu input_entity) sampai ada yang berhasil."""
        galat_terakhir = None
        for d in kandidat_laporan:
            ent = d.entity
            if getattr(ent, 'deactivated', False):
                continue  # grup lama yang sudah dimigrasi ke supergroup
            for tujuan in (ent, d.input_entity):
                try:
                    await client.send_message(tujuan, teks, parse_mode='markdown')
                    return
                except Exception as e:
                    galat_terakhir = e
        raise galat_terakhir or RuntimeError("Tidak ada grup laporan yang valid.")

    if not grup_sumber:
        print(f"❌ Grup sumber '{TARGET_GROUP_SUMBER}' tidak ditemukan!")
        return False
    if not kandidat_laporan:
        print(f"❌ Grup laporan '{TARGET_GROUP_LAPORAN}' tidak ditemukan!")
        return False

    start_date, _ = hitung_rentang_tanggal()
    print(f"📅 Menyedot riwayat Telegram dari tanggal {start_date.strftime('%d-%m-%Y')}...")

    # Tahap 1: kumpulkan pesan & album (grouped_id) yang sudah bertanda sukses
    semua_pesan = []
    album_sukses = set()

    async def iter_semua():
        for g in kandidat_sumber:
            try:
                async for m in client.iter_messages(g):
                    if m.date.astimezone(WIB).date() < start_date:
                        break
                    yield m
            except Exception as e:
                print(f"   ⚠️ Gagal baca satu grup sumber: {e}")

    async for message in iter_semua():
        if type(message).__name__ == 'MessageService':
            continue
        if message.date.astimezone(WIB).date() < start_date:
            break

        semua_pesan.append(message)

        teks_cek = " ".join(t.lower() for t in kumpulkan_teks(message))
        ada_tanda = any(t in teks_cek for t in ["✅", "✔", "👍", "👌", "done"])
        if (ada_reaksi_valid(message) or ada_tanda) and message.grouped_id:
            album_sukses.add(message.grouped_id)
    print(f"🔎 Total pesan terbaca: {len(semua_pesan)}")
    for m in semua_pesan[:5]:
        print("   -", m.date.astimezone(WIB).strftime('%d-%m %H:%M'),
              repr((m.text or '')[:100]), repr(m.file.name if m.file else None))
              
    # Tahap 2: ekstrak SC & tentukan status (pesan dibaca dari terbaru ke terlama)
    status_mentah = {}
    for message in semua_pesan:
        kumpulan = kumpulkan_teks(message)
        if not kumpulan:
            continue

        ada_reaksi = ada_reaksi_valid(message)
        satu_album_sukses = bool(message.grouped_id and message.grouped_id in album_sukses)

        for teks_pesan in kumpulan:
            for baris in teks_pesan.splitlines():
                baris_lower = baris.lower()
                explicit_belum = ("❌" in baris or "✖" in baris or "belum" in baris_lower)
                explicit_sudah = ("✅" in baris or "✔" in baris or "👍" in baris
                                  or "👌" in baris or "done" in baris_lower)

                for no_sc in ekstrak_sc(baris):
                    # Prioritas: reaksi pada pesan mengalahkan tanda ❌ di teks
                    if ada_reaksi:
                        status_baru = "Sudah_Explicit"
                    elif explicit_belum:
                        status_baru = "Belum_Explicit"
                    elif explicit_sudah or satu_album_sukses:
                        status_baru = "Sudah_Explicit"
                    else:
                        status_baru = "Belum_Implicit"

                    status_lama = status_mentah.get(no_sc)
                    if status_lama is None:
                        status_mentah[no_sc] = status_baru
                    elif status_lama == "Belum_Implicit" and status_baru != "Belum_Implicit":
                        # Status "Sudah" tidak akan diturunkan oleh pesan lama tanpa tanda
                        status_mentah[no_sc] = status_baru

    daftar_sc_tele = {
        sc: ("Sudah" if st == "Sudah_Explicit" else "Belum")
        for sc, st in status_mentah.items()
    }
    print(f"📦 Ditemukan {len(daftar_sc_tele)} No. SC unik.")

    list_belum_rekon = [sc for sc, st in daftar_sc_tele.items() if st == "Belum"]
    print(f"📝 Jumlah SC belum rekon: {len(list_belum_rekon)}")

    # Tahap 3: kirim laporan ke grup Telegram
    print(f"📢 Mengirim laporan rekap ke grup '{TARGET_GROUP_LAPORAN}'...")
    try:
        judul = f"📋 **LAPORAN REKON OTOMATIS (Tgl {TANGGAL_MULAI} s.d Hari Ini)**\n\n"
        if list_belum_rekon:
            chunk = judul + "⚠️ **Belum Rekon:**\n"
            for idx, sc in enumerate(list_belum_rekon, 1):
                line = f"{idx}. `{sc}`\n"
                if len(chunk) + len(line) > 3900:
                    await kirim_laporan(chunk)
                    await asyncio.sleep(1)
                    chunk = "⚠️ **(Lanjutan) Belum Rekon:**\n"
                chunk += line
            await kirim_laporan(chunk)
        else:
            await kirim_laporan(judul + "🎉 Mantap! Semua No. SC sudah bersih dan rekon.")
        print("  ✅ Laporan berhasil dikirim ke grup.")
    except Exception as e:
        print(f"  ❌ Gagal kirim pesan ke grup: {e}")
        ada_kegagalan = True

    # Tahap 4: kirim data ke Google Sheets per batch dengan pengulangan otomatis
    data_final = [[sc, st] for sc, st in sorted(daftar_sc_tele.items())]
    if data_final:
        sheets_ok = True
        print(f"📤 Mengirim {len(data_final)} baris ke Google Sheets (batch {BATCH_SIZE})...")
        for i in range(0, len(data_final), BATCH_SIZE):
            payload = {"is_first_batch": (i == 0), "data": data_final[i:i + BATCH_SIZE]}
            berhasil = False
            for percobaan in range(1, MAKS_PERCOBAAN + 1):
                try:
                    r = requests.post(WEB_APP_URL, json=payload, timeout=TIMEOUT_REQUEST)
                    if r.status_code == 200:
                        berhasil = True
                        break
                    print(f"  ⚠️ Batch {i // BATCH_SIZE + 1}, percobaan {percobaan}: status {r.status_code}")
                except Exception as e:
                    print(f"  ⚠️ Batch {i // BATCH_SIZE + 1}, percobaan {percobaan} gagal: {e}")
                await asyncio.sleep(3)

            if not berhasil:
                print(f"  ❌ Batch {i // BATCH_SIZE + 1} gagal setelah {MAKS_PERCOBAAN} percobaan. Dihentikan.")
                ada_kegagalan = True
                sheets_ok = False
                break
            await asyncio.sleep(1.5)

        if sheets_ok:
            print("🎉 Sukses! Data masuk ke Google Spreadsheet.")

    return not ada_kegagalan


with client:
    sukses = client.loop.run_until_complete(main())

if not sukses:
    sys.exit(1)  # Membuat status run di GitHub Actions menjadi merah saat ada kegagalan