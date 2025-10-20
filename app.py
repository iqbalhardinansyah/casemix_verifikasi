import streamlit as st
import pandas as pd
from io import StringIO
import gspread
from google.oauth2.service_account import Credentials
import re

# ====================================
# KONFIGURASI GOOGLE SHEETS
# ====================================
SHEET_URL = "https://docs.google.com/spreadsheets/d/1wLybEwlypYTJQZSQqKfrPDNlX1Uz_Gue6aGKB2dOXR8/edit#gid=0"
WS_RULES = "rules"
WS_DIAG = "diag_groups"
import json

# Membaca kredensial dari Streamlit Secrets (bukan file lokal)
CREDENTIALS_JSON = st.secrets["GOOGLE_CREDENTIALS"]
creds_dict = json.loads(CREDENTIALS_JSON)

def init_sheets():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
        client = gspread.authorize(creds)
        sh = client.open_by_url(SHEET_URL)

        try:
            ws_rules = sh.worksheet(WS_RULES)
        except:
            ws_rules = sh.add_worksheet(title=WS_RULES, rows=1000, cols=10)
            ws_rules.append_row(["Nama Rule", "Kolom", "Isi", "Pesan"])

        try:
            ws_diag = sh.worksheet(WS_DIAG)
        except:
            ws_diag = sh.add_worksheet(title=WS_DIAG, rows=1000, cols=5)
            ws_diag.append_row(["Nama Grup", "DiagList"])

        return sh, ws_rules, ws_diag
    except Exception as e:
        st.error(f"Gagal koneksi ke Google Sheets: {e}")
        return None, None, None

sh, ws_rules, ws_diag = init_sheets()

# ====================================
# KONFIGURASI STREAMLIT
# ====================================
st.set_page_config(page_title="E-Klaim Verif", layout="wide", initial_sidebar_state="expanded")

if "data" not in st.session_state:
    st.session_state.data = None

# ====================================
# BACA FILE
# ====================================
def detect_sep(line: str):
    if "|" in line: return "|"
    if ";" in line: return ";"
    if "\t" in line: return "\t"
    return r"\s+"

def parse_txt(uploaded_file):
    content = uploaded_file.read()
    try:
        text = content.decode("utf-8")
    except:
        text = content.decode("latin1", errors="ignore")

    sep = detect_sep(text.splitlines()[0])
    df = pd.read_csv(StringIO(text), sep=sep, engine="python", dtype=str, keep_default_na=False)
    df = df.applymap(lambda x: x.strip() if isinstance(x, str) else x)
    return df

# ====================================
# LOAD RULES & DIAG GROUPS
# ====================================
def load_rules():
    if ws_rules is None: return []
    try: return ws_rules.get_all_records()
    except Exception as e:
        st.error(f"Gagal memuat rules: {e}")
        return []

def load_diag():
    if ws_diag is None: return []
    try:
        recs = ws_diag.get_all_records()
        out = []
        for r in recs:
            out.append({
                "nama": r.get("Nama Grup", ""),
                "daftar": [x.strip() for x in str(r.get("DiagList", "")).split(",") if x.strip()]
            })
        return out
    except Exception as e:
        st.error(f"Gagal memuat grup diagnosa: {e}")
        return []

# ====================================
# FUNGSI RULE
# ====================================
def add_note(df, mask, text):
    if "NOTE" not in df.columns:
        df["NOTE"] = ""
    df.loc[mask, "NOTE"] = df.loc[mask, "NOTE"].apply(
        lambda cur: (cur + " | " + text) if (cur and str(cur).strip()) else text
    )

def re_split_vals(s):
    if not isinstance(s, str): return []
    for sep in [";", ",", "|"]:
        if sep in s: return s.split(sep)
    return [s]

def apply_manual_rules(df, rules):
    import pandas as pd

    # Jika rules masih berbentuk list, ubah jadi DataFrame
    if isinstance(rules, list):
        if len(rules) > 0 and isinstance(rules[0], dict):
            rules = pd.DataFrame(rules)
        else:
            return df  # kalau kosong, langsung kembalikan df

    for _, rule in rules.iterrows():
        kol = rule.get("Kolom", "")
        isi = str(rule.get("Isi", "")).strip()
        pesan = rule.get("Pesan", "")

        if not kol or kol not in df.columns:
            continue

        # Jika kolom DIAGLIST dan isi berisi banyak kode (misal: J18.9;J44.0)
        if kol == "DIAGLIST" and ";" in isi:
            target_kode = [i.strip() for i in isi.split(";") if i.strip()]
            for i, row in df.iterrows():
                diaglist = str(row["DIAGLIST"]).split(";")
                # Semua kode di rule harus muncul di DIAGLIST pasien
                if all(kode in diaglist for kode in target_kode):
                    df.at[i, "NOTE"] += f"| {pesan}"

        # Untuk rule biasa (bukan gabungan DIAGLIST)
        else:
            mask = df[kol].astype(str).str.contains(isi, case=False, na=False)
            df.loc[mask, "NOTE"] += f"| {pesan}"

    return df


def apply_los(df):
    if "PTD" in df.columns and "LOS" in df.columns:
        df["LOS_num"] = pd.to_numeric(df["LOS"], errors="coerce")
        mask = (df["PTD"].astype(str) == "1") & (df["LOS_num"].notna()) & (df["LOS_num"] <= 2)
        add_note(df, mask, "Potensi pending LOS ≤ 2")
        df.drop(columns=["LOS_num"], inplace=True, errors="ignore")
    return df

# 🔁 Readmisi (dengan tanggal)
def apply_readmisi(df, diag_groups):
    need = {"MRN", "ADMISSION_DATE", "DISCHARGE_DATE", "DIAGLIST", "PTD"}
    if not need.issubset(df.columns):
        return df

    # parse dengan dayfirst=True supaya format DD/MM/YYYY dibaca benar
    df["ADMISSION_parsed"] = pd.to_datetime(df["ADMISSION_DATE"], dayfirst=True, errors="coerce")
    df["DISCHARGE_parsed"] = pd.to_datetime(df["DISCHARGE_DATE"], dayfirst=True, errors="coerce")

    for mrn, group in df.groupby("MRN"):
        group = group.sort_values("ADMISSION_parsed")
        prev_idx = None
        for idx, row in group.iterrows():
            if prev_idx is not None:
                prev = df.loc[prev_idx]
                if str(prev.get("PTD", "")).strip() == "1" and str(row.get("PTD", "")).strip() == "1":
                    d_prev = prev["DISCHARGE_parsed"]
                    d_cur = row["ADMISSION_parsed"]
                    if pd.notna(d_prev) and pd.notna(d_cur):
                        days = (d_cur - d_prev).days
                        if days <= 30 and days >= 0:
                            diag_prev = str(prev.get("DIAGLIST", "")).strip()
                            diag_cur = str(row.get("DIAGLIST", "")).strip()
                            # diagnosa sama persis atau diag_prev ada di diag_cur (multi DIAGLIST dipisah ;)
                            if diag_prev and (diag_prev == diag_cur or diag_prev in diag_cur.split(";")):
                                note = f"Potensi Readmisi (Pulang: {d_prev.date()} → Masuk: {d_cur.date()})"
                                add_note(df, df.index == idx, note)
                            else:
                                # cek grup diagnosa (jika ada)
                                for g in diag_groups:
                                    if diag_prev in g.get("daftar", []) and diag_cur in g.get("daftar", []):
                                        note = f"Readmisi {diag_prev}→{diag_cur} (Pulang: {d_prev.date()} → Masuk: {d_cur.date()})"
                                        add_note(df, df.index == idx, note)
                                        break
            prev_idx = idx

    df.drop(columns=["ADMISSION_parsed", "DISCHARGE_parsed"], inplace=True, errors="ignore")
    return df


# 💊 Fragmentasi (dengan tanggal)
def apply_fragmentasi(df):
    need = {"MRN", "ADMISSION_DATE", "DISCHARGE_DATE", "DIAGLIST", "PTD"}
    if not need.issubset(df.columns):
        return df

    # parse dengan dayfirst=True
    df["ADMISSION_parsed"] = pd.to_datetime(df["ADMISSION_DATE"], dayfirst=True, errors="coerce")
    df["DISCHARGE_parsed"] = pd.to_datetime(df["DISCHARGE_DATE"], dayfirst=True, errors="coerce")

    for mrn, group in df.groupby("MRN"):
        group = group.sort_values("ADMISSION_parsed")
        prev_idx = None
        for idx, row in group.iterrows():
            if prev_idx is not None:
                prev = df.loc[prev_idx]
                if str(prev.get("PTD", "")).strip() == "2" and str(row.get("PTD", "")).strip() == "2":
                    d_prev = prev["DISCHARGE_parsed"]
                    d_cur = row["ADMISSION_parsed"]
                    if pd.notna(d_prev) and pd.notna(d_cur):
                        days = (d_cur - d_prev).days
                        # hanya hitung fragmentasi jika selisih hari >0 dan <=7
                        if 0 < days <= 7:
                            diag_prev = str(prev.get("DIAGLIST", "")).strip()
                            diag_cur = str(row.get("DIAGLIST", "")).strip()
                            if diag_prev and diag_prev == diag_cur:
                                note = f"Potensi Fragmentasi (Kunjungan Sebelumnya: {d_prev.date()} → {d_cur.date()})"
                                add_note(df, df.index == idx, note)
            prev_idx = idx

    df.drop(columns=["ADMISSION_parsed", "DISCHARGE_parsed"], inplace=True, errors="ignore")
    return df

# ====================================
# SIDEBAR
# ====================================
with st.sidebar:
    st.title("E-Klaim Verif")
    menu = st.radio("Navigasi", ["Dashboard", "Upload File", "Eklaim Data", "Rules", "Hasil Verifikasi"])
    st.markdown("---")
    st.caption("© 2025-Iqbal Hardinansyah, AMd.Kes")

# ====================================
# MENU UPLOAD FILE
# ====================================
if menu == "Upload File":
    st.header("📂 Upload File E-Klaim")
    uploaded = st.file_uploader("Pilih file TXT/CSV", type=["txt", "csv"])
    if uploaded:
        df = parse_txt(uploaded)
        st.session_state.data = df
        st.success(f"File berhasil dimuat: {len(df)} baris.")
        st.dataframe(df.head())
    st.caption("© 2025-Iqbal Hardinansyah, AMd.Kes")

# ====================================
# MENU EKLAIM DATA
# ====================================
elif menu == "Eklaim Data":
    st.header("📋 Data E-Klaim")
    if st.session_state.data is None:
        st.warning("Belum ada data.")
    else:
        df = st.session_state.data.copy()
        if "INACBG" in df.columns:
            parts = df["INACBG"].str.split("-", expand=True)
            if parts.shape[1] >= 4:
                df["CMG"], df["CASETYPE"], df["CBG"], df["SL"] = parts[0], parts[1], parts[2], parts[3]
        if "TOTAL_TARIF" in df.columns and "TARIF_RS" in df.columns:
            df["Selisih"] = pd.to_numeric(df["TOTAL_TARIF"], errors="coerce") - pd.to_numeric(df["TARIF_RS"], errors="coerce")
        st.dataframe(df, use_container_width=True)
    st.caption("© 2025-Iqbal Hardinansyah, AMd.Kes")

# ====================================
# MENU RULES
# ====================================
elif menu == "Rules":
    st.header("⚙️ Aturan Verifikasi & Grup Diagnosa Readmisi")
    st.info("Tambah, hapus, atau ubah rules dan grup diagnosa langsung dari sini.")

    rules = load_rules()
    diags = load_diag()

    # ===============================
    # FORM TAMBAH RULE BARU
    # ===============================
    st.subheader("Tambah Rule Baru")
    with st.form("add_rule"):
        nama = st.text_input("Nama Rule")
        kolom = st.text_input("Kolom (harus sesuai kolom di file, contoh: DIAGLIST)")
        isi = st.text_input("Isi (pisahkan ; atau ,)")
        pesan = st.text_area("Pesan / Catatan")
        if st.form_submit_button("Simpan Rule"):
            ws_rules.append_row([nama, kolom, isi, pesan])
            st.success("✅ Rule berhasil disimpan ke Google Sheets.")
            st.experimental_rerun()

    # ===============================
    # DAFTAR RULES + FITUR HAPUS
    # ===============================
    st.markdown("---")
    st.subheader("📋 Daftar Rules")
    if rules:
        df_rules = pd.DataFrame(rules)
        for i, row in df_rules.iterrows():
            cols = st.columns([3, 2, 2, 3, 1])
            cols[0].write(row["Nama Rule"])
            cols[1].write(row["Kolom"])
            cols[2].write(row["Isi"])
            cols[3].write(row["Pesan"])
            if cols[4].button("🗑️ Hapus", key=f"del_rule_{i}"):
                ws_rules.delete_rows(i + 2)  # +2 karena header di baris 1
                st.success(f"Rule '{row['Nama Rule']}' berhasil dihapus.")
                st.experimental_rerun()
    else:
        st.info("Belum ada rule tersimpan.")

    # ===============================
    # FORM TAMBAH GRUP DIAGNOSA
    # ===============================
    st.markdown("---")
    st.subheader("Tambah Grup Diagnosa (untuk Readmisi)")
    with st.form("add_diag"):
        nama_g = st.text_input("Nama Grup Diagnosa")
        daftar_g = st.text_input("Daftar Diagnosa (pisahkan koma, contoh: Z09.8,J10,I10)")
        if st.form_submit_button("Simpan Grup"):
            ws_diag.append_row([nama_g, daftar_g])
            st.success("✅ Grup diagnosa berhasil disimpan.")
            st.experimental_rerun()

    # ===============================
    # DAFTAR GRUP DIAGNOSA + FITUR HAPUS
    # ===============================
    st.markdown("---")
    st.subheader("📑 Daftar Grup Diagnosa")
    if diags:
        for i, g in enumerate(diags):
            cols = st.columns([3, 6, 1])
            cols[0].write(g["nama"])
            cols[1].write(", ".join(g["daftar"]))
            if cols[2].button("❌ Hapus", key=f"del_diag_{i}"):
                ws_diag.delete_rows(i + 2)
                st.success(f"Grup '{g['nama']}' berhasil dihapus.")
                st.experimental_rerun()
    else:
        st.info("Belum ada grup diagnosa tersimpan.")

    st.caption("© 2025-Iqbal Hardinansyah, AMd.Kes")


# ====================================
# MENU DASHBOARD
# ====================================
elif menu == "Dashboard":
    st.header("📊 Dashboard E-Klaim")
    if st.session_state.data is None:
        st.warning("Belum ada data diupload.")
    else:
        df = st.session_state.data.copy()
        df["TOTAL_TARIF"] = pd.to_numeric(df.get("TOTAL_TARIF", 0), errors="coerce").fillna(0)
        df["TARIF_RS"] = pd.to_numeric(df.get("TARIF_RS", 0), errors="coerce").fillna(0)

        rules = load_rules()
        diags = load_diag()

        df["NOTE"] = ""
        df = apply_los(df)
        df = apply_manual_rules(df, rules)
        df = apply_readmisi(df, diags)
        df = apply_fragmentasi(df)

        total_klaim = len(df)
        total_tarif = df["TOTAL_TARIF"].sum()
        total_rs = df["TARIF_RS"].sum()
        selisih = total_tarif - total_rs
        total_ranap = (df["PTD"] == "1").sum() if "PTD" in df.columns else 0
        total_rajal = (df["PTD"] == "2").sum() if "PTD" in df.columns else 0
        total_pending = df["NOTE"].astype(bool).sum()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📑Total Klaim", f"{total_klaim:,}")
        c2.metric("💰Total Tarif", f"Rp {total_tarif:,.0f}")
        c3.metric("🏥Total Tarif RS", f"Rp {total_rs:,.0f}")
        c4.metric("⚖️Selisih Tarif", f"Rp {selisih:,.0f}")

        c5, c6, c7 = st.columns(3)
        c5.metric("🛏Total Ranap", f"{total_ranap:,}")
        c6.metric("👨‍👦Total Rajal", f"{total_rajal:,}")
        c7.metric("⏳Total Potensi Pending", f"{total_pending:,}")

        st.subheader("20 Diagnosa Terbanyak")
        if "DIAGLIST" in df.columns:
            diag_counts = df["DIAGLIST"].value_counts().head(20).reset_index()
            diag_counts.columns = ["Diagnosa", "Jumlah"]
            st.dataframe(diag_counts)

        st.subheader("20 Tindakan Terbanyak")
        if "PROCLIST" in df.columns:
            proc_counts = df["PROCLIST"].value_counts().head(20).reset_index()
            proc_counts.columns = ["Tindakan", "Jumlah"]
            st.dataframe(proc_counts)

    st.caption("© 2025-Iqbal Hardinansyah, AMd.Kes")

# ====================================
# MENU HASIL VERIFIKASI
# ====================================
elif menu == "Hasil Verifikasi":
    st.header("✅ Hasil Verifikasi")
    if st.session_state.data is None:
        st.warning("Silakan upload file terlebih dahulu.")
    else:
        df = st.session_state.data.copy()
        rules = load_rules()
        diags = load_diag()

        df["NOTE"] = ""
        df = apply_los(df)
        df = apply_manual_rules(df, rules)
        df = apply_readmisi(df, diags)
        df = apply_fragmentasi(df)

        df_verif = df[df["NOTE"].astype(bool)].copy()
        
    # --- Mapping PTD dan DISCHARGE_STATUS ---
    if not df_verif.empty:
        # Ubah PTD jadi teks
        df_verif["PTD"] = df_verif["PTD"].replace({
            1: "Ranap",
            2: "Rajal",
            "1": "Ranap",
            "2": "Rajal"
        })

        # Tambah kolom DISCHARGE_STATUS (mapping dari angka ke teks)
        if "DISCHARGE_STATUS" in df_verif.columns:
            df_verif["DISCHARGE_STATUS"] = df_verif["DISCHARGE_STATUS"].replace({
                1: "Atas Persetujuan Dr.",
                2: "Dirujuk",
                3: "Atas Permintaan Sendiri",
                4: "Meninggal",
                "1": "Atas Persetujuan Dr.",
                "2": "Dirujuk",
                "3": "Atas Permintaan Sendiri",
                "4": "Meninggal"
            })

        if df_verif.empty:
            st.info("Tidak ada hasil verifikasi.")
        else:
            unique_notes = sorted({p.strip() for n in df_verif["NOTE"] for p in n.split("|") if p.strip()})
            selected = st.multiselect("Filter berdasarkan Note:", unique_notes)
            if selected:
                mask = df_verif["NOTE"].apply(lambda x: any(s in x for s in selected))
                df_verif = df_verif[mask]

            tampil = ["NOTE", "KELAS_RAWAT", "PTD", "ADMISSION_DATE", "DISCHARGE_DATE",
                      "DIAGLIST", "PROCLIST", "NAMA_PASIEN", "MRN", "SEP", "DISCHARGE_STATUS"]
            tampil = [c for c in tampil if c in df_verif.columns]
            st.dataframe(df_verif[tampil], use_container_width=True)
                # --- Tombol download CSV & Excel ---
        import io

        csv_data = df_verif.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Download CSV",
            data=csv_data,
            file_name="hasil_verifikasi.csv",
            mime="text/csv"
        )

        # Untuk Excel
        import pandas as pd
        from io import BytesIO
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df_verif.to_excel(writer, index=False, sheet_name="Hasil Verifikasi")
        st.download_button(
            label="📊 Download Excel",
            data=output.getvalue(),
            file_name="hasil_verifikasi.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    
    st.caption("© 2025-Iqbal Hardinansyah, AMd.Kes")
