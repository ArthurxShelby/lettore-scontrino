from datetime import datetime, timedelta
import io
import os
import re
from typing import List, Optional

from google import genai
from google.genai import types
import pandas as pd
from PIL import Image
from pydantic import BaseModel, Field
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
import streamlit as st
from supabase import Client, create_client

# Librerie per l'estrazione tabelle da PDF locale
try:
  import pdfplumber
except ImportError:
  pdfplumber = None

try:
  import pypdf
except ImportError:
  pypdf = None

# Configurazione della pagina Streamlit
st.set_page_config(page_title="Gestione Bilancio & Scontrini", layout="wide")


# --- SISTEMA DI AUTENTICAZIONE CON PASSWORD ---
def verifica_password() -> bool:
  if "autenticato" not in st.session_state:
    st.session_state["autenticato"] = False

  if st.session_state["autenticato"]:
    return True

  st.title("🔒 Accesso Riservato")
  st.write("Inserisci la password per accedere al sistema di gestione bilancio.")

  password_corretta = st.secrets.get("APP_PASSWORD") or os.environ.get(
      "APP_PASSWORD"
  )

  if not password_corretta:
    st.error(
        "⚠️ La password non è stata configurata nei secrets ('APP_PASSWORD')."
    )
    return False

  with st.form("form_login"):
    password_inserita = st.text_input("Password", type="password")
    submit_login = st.form_submit_button("Accedi", type="primary")

    if submit_login:
      if password_inserita == password_corretta:
        st.session_state["autenticato"] = True
        st.success("Accesso effettuato!")
        st.rerun()
      else:
        st.error("❌ Password errata. Riprova.")

  return False


if not verifica_password():
  st.stop()


# --- INIZIO APPLICAZIONE (AUTENTICATA) ---
st.title("🧾 Gestione Entrate, Uscite e Riconciliazione (Supabase)")

with st.sidebar:
  st.write("👤 Sessione Attiva")
  if st.button("🚪 Disconnetti", use_container_width=True):
    st.session_state["autenticato"] = False
    st.rerun()


# --- INIZIALIZZAZIONE SUPABASE ---
@st.cache_resource
def init_supabase() -> Client:
  url = st.secrets.get("SUPABASE_URL") or os.environ.get("SUPABASE_URL")
  key = st.secrets.get("SUPABASE_KEY") or os.environ.get("SUPABASE_KEY")
  return create_client(url, key)


supabase = init_supabase()


# Schemi Pydantic per i dati strutturati
class ScontrinoData(BaseModel):
  nome_negozio: str = Field(description="Nome dell'esercente")
  data: Optional[str] = Field(
      default=None,
      description="Data dello scontrino (YYYY-MM-DD) se visibile, altrimenti null",
  )
  totale_euro: float = Field(description="Importo totale finale pagato in Euro")


class VocaleData(BaseModel):
  negozio: str = Field(
      description="Descrizione della spesa/entrata o nome del negozio"
  )
  totale: float = Field(description="Importo in euro menzionato nell'audio")
  tipo: str = Field(description="Tipo: 'Entrata' o 'Uscita'")
  data: Optional[str] = Field(
      default=None,
      description="Data in formato YYYY-MM-DD se specificata, altrimenti null",
  )


class MovimentoBancarioPDF(BaseModel):
  data: str = Field(description="Data della transazione in formato YYYY-MM-DD")
  descrizione: str = Field(
      description="Causale o descrizione del movimento bancario"
  )
  importo: float = Field(description="Importo assoluto in euro (valore positivo)")


class EstrattoContoPDFData(BaseModel):
  movimenti: List[MovimentoBancarioPDF] = Field(
      description="Lista delle transazioni trovate nel documento bancario"
  )


# --- FUNZIONI SUPABASE ---
def carica_storico() -> list:
  try:
    response = (
        supabase.table("movimenti")
        .select("*")
        .order("data", desc=True)
        .execute()
    )
    return response.data
  except Exception as e:
    st.error(f"Errore durante il caricamento da Supabase: {e}")
    return []


def salva_movimento(
    negozio: str, data: str, totale: float, tipo: str = "Uscita"
):
  try:
    data_payload = {
        "negozio": negozio,
        "data": data,
        "totale": totale,
        "tipo": tipo,
    }
    supabase.table("movimenti").insert(data_payload).execute()
  except Exception as e:
    st.error(f"Errore durante il salvataggio su Supabase: {e}")


def aggiorna_movimento(
    item_id: int, negozio: str, data: str, totale: float, tipo: str
):
  try:
    data_payload = {
        "negozio": negozio,
        "data": data,
        "totale": totale,
        "tipo": tipo,
    }
    supabase.table("movimenti").update(data_payload).eq("id", item_id).execute()
  except Exception as e:
    st.error(f"Errore durante l'aggiornamento su Supabase: {e}")


def elimina_movimento(item_id: int):
  try:
    supabase.table("movimenti").delete().eq("id", item_id).execute()
  except Exception as e:
    st.error(f"Errore durante l'eliminazione da Supabase: {e}")


# --- ESTRAZIONE ROBUSTA LOCALE DA PDF (pdfplumber + pypdf) ---
def estrai_movimenti_pdf_locale(file_bytes: bytes) -> list:
  movimenti = []

  # Metodo 1: Estrazione tabelle strutturate con pdfplumber
  if pdfplumber is not None:
    try:
      with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
          tables = page.extract_tables()
          for table in tables:
            for row in table:
              if not row or len(row) < 2:
                continue
              row_str = " ".join([str(cell) for cell in row if cell])

              # Cerca date (DD/MM/YYYY o DD-MM-YYYY) e importi (es. 12,34 o 1.250,50)
              data_match = re.search(
                  r"(\d{2}[/\.-]\d{2}[/\.-]\d{2,4})", row_str
              )
              imp_match = re.search(r"(-?\d+(?:\.\d{3})*,\d{2})", row_str)

              if data_match and imp_match:
                d_str = data_match.group(1)
                i_str = imp_match.group(1)

                try:
                  d_clean = datetime.strptime(
                      d_str.replace(".", "/").replace("-", "/"), "%d/%m/%Y"
                  ).strftime("%Y-%m-%d")
                except Exception:
                  continue

                imp_clean = abs(
                    float(i_str.replace(".", "").replace(",", "."))
                )
                desc_clean = (
                    row_str.replace(d_str, "").replace(i_str, "").strip()
                )

                movimenti.append({
                    "data": d_clean,
                    "descrizione": desc_clean if desc_clean else "Movimento Bancario",
                    "importo": imp_clean,
                })
    except Exception:
      pass

  # Metodo 2: Fallback su pypdf con regex flessibile per righe di testo
  if not movimenti and pypdf is not None:
    try:
      reader = pypdf.PdfReader(io.BytesIO(file_bytes))
      testo_completo = ""
      for page in reader.pages:
        testo_completo += page.extract_text() + "\n"

      linee = testo_completo.split("\n")
      for linea in linee:
        data_match = re.search(r"(\d{2}[/\.-]\d{2}[/\.-]\d{2,4})", linea)
        imp_match = re.search(r"(-?\d+(?:\.\d{3})*,\d{2})", linea)

        if data_match and imp_match:
          d_str = data_match.group(1)
          i_str = imp_match.group(1)

          try:
            d_clean = datetime.strptime(
                d_str.replace(".", "/").replace("-", "/"), "%d/%m/%Y"
            ).strftime("%Y-%m-%d")
          except Exception:
            continue

          imp_clean = abs(float(i_str.replace(".", "").replace(",", ".")))
          desc_clean = linea.replace(d_str, "").replace(i_str, "").strip()

          movimenti.append({
              "data": d_clean,
              "descrizione": desc_clean if desc_clean else "Movimento Bancario",
              "importo": imp_clean,
          })
    except Exception:
      pass

  return movimenti


# --- GENERAZIONE PDF REPORT BILANCIO ---
def genera_pdf_storico(storico: list) -> bytes:
  buffer = io.BytesIO()
  doc = SimpleDocTemplate(
      buffer,
      pagesize=A4,
      rightMargin=30,
      leftMargin=30,
      topMargin=30,
      bottomMargin=30,
  )
  story = []
  styles = getSampleStyleSheet()

  title = Paragraph("<b>Report Bilancio: Entrate e Uscite</b>", styles["Heading1"])
  story.append(title)

  data_generazione = Paragraph(
      f"<i>Generato il: {datetime.now().strftime('%d/%m/%Y alle %H:%M')}</i>",
      styles["Normal"],
  )
  story.append(data_generazione)
  story.append(Spacer(1, 15))

  tot_entrate = sum(
      float(item["totale"])
      for item in storico
      if item.get("tipo") == "Entrata"
  )
  tot_uscite = sum(
      float(item["totale"]) for item in storico if item.get("tipo") == "Uscita"
  )
  saldo = tot_entrate - tot_uscite

  table_data = [["Data", "Descrizione / Negozio", "Tipo", "Importo (€)"]]

  for item in storico:
    tipo = item.get("tipo", "Uscita")
    segno = "+" if tipo == "Entrata" else "-"
    table_data.append([
        str(item["data"]),
        item["negozio"],
        tipo,
        f"{segno} € {float(item['totale']):.2f}",
    ])

  table_data.append(["TOTALE ENTRATE", "", "", f"+ € {tot_entrate:.2f}"])
  table_data.append(["TOTALE USCITE", "", "", f"- € {tot_uscite:.2f}"])
  table_data.append(["SALDO NETTO", "", "", f"€ {saldo:.2f}"])

  table_style = TableStyle([
      ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#31333F")),
      ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
      ("ALIGN", (0, 0), (-1, -1), "LEFT"),
      ("ALIGN", (3, 0), (3, -1), "RIGHT"),
      ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
      ("FONTSIZE", (0, 0), (-1, -1), 10),
      ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
      ("GRID", (0, 0), (-1, -4), 0.5, colors.grey),
      ("BACKGROUND", (0, -3), (-1, -1), colors.HexColor("#F0F2F6")),
      ("FONTNAME", (0, -3), (-1, -1), "Helvetica-Bold"),
      ("SPAN", (0, -3), (2, -3)),
      ("SPAN", (0, -2), (2, -2)),
      ("SPAN", (0, -1), (2, -1)),
  ])

  table = Table(table_data, colWidths=[80, 240, 80, 120])
  table.setStyle(table_style)
  story.append(table)

  doc.build(story)
  buffer.seek(0)
  return buffer.getvalue()


# --- INTERFACCIA APP STREAMLIT ---
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📷 Scansiona Scontrino",
    "🎤 Inserimento Vocale",
    "✍️ Inserimento Manuale",
    "📊 Bilancio & Export PDF",
    "🔍 Riconciliazione Bancaria",
])

# TAB 1: ACQUISIZIONE FOTO OTTIMIZZATA
with tab1:
  if "camera_attiva" not in st.session_state:
    st.session_state["camera_attiva"] = False

  col_cam1, col_cam2 = st.columns([1, 1])

  with col_cam1:
    if st.button("📷 Attiva Fotocamera", use_container_width=True):
      st.session_state["camera_attiva"] = True

  with col_cam2:
    if st.session_state["camera_attiva"]:
      if st.button("🚫 Disattiva Fotocamera", use_container_width=True):
        st.session_state["camera_attiva"] = False
        st.rerun()

  foto_scattata = None
  if st.session_state["camera_attiva"]:
    foto_scattata = st.camera_input("Scatta una foto allo scontrino")

  if foto_scattata is not None:
    immagine = Image.open(foto_scattata)
    immagine.thumbnail((1024, 1024))

    if st.button("Analizza e Salva come Uscita", type="primary"):
      with st.spinner("Analisi veloce in corso..."):
        api_key = st.secrets.get("GEMINI_API_KEY") or os.environ.get(
            "GEMINI_API_KEY"
        )
        client = genai.Client(api_key=api_key)

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ScontrinoData,
            temperature=0.1,
        )
        prompt = (
            "Analizza lo scontrino. Estrai nome negozio, data (YYYY-MM-DD) e"
            " totale finale in euro. Se la data non è visibile, usa null."
        )

        modelli = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
        dati = None

        for modello in modelli:
          try:
            response = client.models.generate_content(
                model=modello,
                contents=[immagine, prompt],
                config=config,
            )
            dati = response.parsed
            if dati is not None:
              break
          except Exception:
            continue

        if dati is not None:
          data_finale = (
              dati.data if dati.data else datetime.now().strftime("%Y-%m-%d")
          )
          salva_movimento(
              dati.nome_negozio, data_finale, dati.totale_euro, tipo="Uscita"
          )

          st.success("Scontrino salvato in Supabase!")
          st.metric("Spesa Registrata", f"€ {dati.totale_euro:.2f}")
          st.write(f"**Negozio:** {dati.nome_negozio} | **Data:** {data_finale}")
        else:
          st.error(
              "Impossibile analizzare l'immagine. Riprova tra qualche secondo."
          )

# TAB 2: INSERIMENTO VOCALE OTTIMIZZATO
with tab2:
  st.subheader("Registra una nota vocale")
  st.write(
      "Esempio: *'Speso 15.50 euro al bar per colazione'* o *'Incassato 200 euro"
      " consulenza'*"
  )

  if "audio_key" not in st.session_state:
    st.session_state["audio_key"] = 0

  if "dati_vocali_temp" not in st.session_state:
    st.session_state["dati_vocali_temp"] = None

  audio_registrato = st.audio_input(
      "Premi il microfono per registrare",
      key=f"audio_input_{st.session_state['audio_key']}",
  )

  if audio_registrato is not None:
    if st.button("🎙️ Analizza Audio", type="primary"):
      with st.spinner("Analisi audio rapida..."):
        api_key = st.secrets.get("GEMINI_API_KEY") or os.environ.get(
            "GEMINI_API_KEY"
        )
        client = genai.Client(api_key=api_key)

        audio_bytes = audio_registrato.read()
        mime_type = audio_registrato.type or "audio/wav"
        part_audio = types.Part.from_bytes(
            data=audio_bytes, mime_type=mime_type
        )

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=VocaleData,
            temperature=0.1,
        )

        prompt_vocale = (
            "Estrai dall'audio: descrizione spesa/entrata, importo in euro,"
            " tipo ('Entrata' o 'Uscita') e data YYYY-MM-DD (se menzionata,"
            " altrimenti null)."
        )

        modelli = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
        dati_vocali = None

        for modello in modelli:
          try:
            response = client.models.generate_content(
                model=modello,
                contents=[part_audio, prompt_vocale],
                config=config,
            )
            dati_vocali = response.parsed
            if dati_vocali is not None:
              break
          except Exception:
            continue

        if dati_vocali is not None:
          st.session_state["dati_vocali_temp"] = {
              "negozio": dati_vocali.negozio,
              "totale": dati_vocali.totale,
              "tipo": dati_vocali.tipo,
              "data": (
                  dati_vocali.data
                  if dati_vocali.data
                  else datetime.now().strftime("%Y-%m-%d")
              ),
          }
        else:
          st.error(
              "Errore nell'elaborazione dell'audio. Quota momentaneamente"
              " satura, riprova tra poco."
          )

  if st.session_state["dati_vocali_temp"] is not None:
    dati = st.session_state["dati_vocali_temp"]
    st.info("🔍 **Anteprima Dati Riconosciuti:**")

    col_v1, col_v2 = st.columns(2)
    with col_v1:
      st.write(f"**Descrizione:** {dati['negozio']}")
      st.write(f"**Tipo:** {dati['tipo']}")
    with col_v2:
      st.write(f"**Importo:** € {dati['totale']:.2f}")
      st.write(f"**Data:** {dati['data']}")

    if st.button("💾 Conferma e Salva Movimento", type="primary"):
      salva_movimento(
          negozio=dati["negozio"],
          data=dati["data"],
          totale=dati["totale"],
          tipo=dati["tipo"],
      )
      st.session_state["dati_vocali_temp"] = None
      st.session_state["audio_key"] += 1
      st.success("Movimento salvato!")
      st.rerun()

# TAB 3: INSERIMENTO MANUALE
with tab3:
  st.subheader("Inserisci un'Entrata o un'Uscita")

  with st.form("form_inserimento_manuale", clear_on_submit=True):
    m_tipo = st.radio(
        "Tipo Movimento", ["Uscita (Spesa)", "Entrata (Ricavo)"], horizontal=True
    )
    m_negozio = st.text_input(
        "Descrizione / Esercente",
        placeholder="Es. Stipendio, Rimborso, Bar Centrale",
    )
    m_data = st.date_input("Data", value=datetime.now())
    m_totale = st.number_input(
        "Importo Totale (€)", min_value=0.01, step=0.10, format="%.2f"
    )

    submit_manuale = st.form_submit_button(
        "➕ Salva Movimento", type="primary"
    )

    if submit_manuale:
      if m_negozio.strip() == "":
        st.error("Inserisci una descrizione o il nome dell'esercente.")
      else:
        tipo_str = "Entrata" if "Entrata" in m_tipo else "Uscita"
        data_str = m_data.strftime("%Y-%m-%d")
        salva_movimento(m_negozio, data_str, float(m_totale), tipo=tipo_str)
        st.success(f"Registrata {tipo_str}: **{m_negozio}** - € {m_totale:.2f}")

# TAB 4: BILANCIO E TABELLA
with tab4:
  storico_attuale = carica_storico()

  if storico_attuale:
    st.subheader("📊 Bilancio in Tempo Reale")

    tot_entrate = sum(
        float(item["totale"])
        for item in storico_attuale
        if item.get("tipo") == "Entrata"
    )
    tot_uscite = sum(
        float(item["totale"])
        for item in storico_attuale
        if item.get("tipo") == "Uscita"
    )
    saldo = tot_entrate - tot_uscite

    col_m1, col_m2, col_m3 = st.columns(3)
    col_m1.metric("🟢 Entrate Totali", f"€ {tot_entrate:.2f}")
    col_m2.metric("🔴 Uscite Totali", f"€ {tot_uscite:.2f}")
    col_m3.metric("⚖️ Saldo Netto", f"€ {saldo:.2f}")

    st.divider()

    col_titolo, col_pdf = st.columns([2, 1])
    with col_titolo:
      st.subheader("📋 Registro Movimenti")
    with col_pdf:
      pdf_bytes = genera_pdf_storico(storico_attuale)
      st.download_button(
          label="📄 Scarica PDF",
          data=pdf_bytes,
          file_name=f"report_bilancio_{datetime.now().strftime('%Y%m%d')}.pdf",
          mime="application/pdf",
          type="primary",
          use_container_width=True,
      )

    h_col1, h_col2, h_col3, h_col4, h_col5 = st.columns([1.2, 2.5, 1, 1.2, 0.6])
    with h_col1:
      st.markdown("**Data**")
    with h_col2:
      st.markdown("**Descrizione / Negozio**")
    with h_col3:
      st.markdown("**Tipo**")
    with h_col4:
      st.markdown("**Importo**")
    with h_col5:
      st.markdown("**Azione**")

    st.markdown(
        "<hr style='margin-top:2px; margin-bottom:8px; border:1px solid"
        " #31333F;'>",
        unsafe_allow_html=True,
    )

    for item in storico_attuale:
      item_id = item["id"]
      tipo = item.get("tipo", "Uscita")
      colore_ico = "🟢" if tipo == "Entrata" else "🔴"
      segno = "+" if tipo == "Entrata" else "-"

      r_col1, r_col2, r_col3, r_col4, r_col5 = st.columns(
          [1.2, 2.5, 1, 1.2, 0.6]
      )

      with r_col1:
        st.write(f"`{item['data']}`")
      with r_col2:
        st.write(item["negozio"])
      with r_col3:
        st.write(f"{colore_ico} {tipo}")
      with r_col4:
        st.write(f"**{segno} € {float(item['totale']):.2f}**")
      with r_col5:
        if st.button("✏️", key=f"edit_btn_{item_id}", help="Modifica / Elimina"):
          st.session_state[f"editing_{item_id}"] = not st.session_state.get(
              f"editing_{item_id}", False
          )

      if st.session_state.get(f"editing_{item_id}", False):
        with st.container():
          with st.form(key=f"form_edit_{item_id}"):
            st.caption(f"✏️ Modifica Movimento ID: #{item_id}")
            f_col1, f_col2 = st.columns(2)
            with f_col1:
              nuovo_tipo = st.selectbox(
                  "Tipo",
                  ["Uscita", "Entrata"],
                  index=0 if tipo == "Uscita" else 1,
              )
              nuovo_negozio = st.text_input(
                  "Descrizione/Negozio", value=item["negozio"]
              )
            with f_col2:
              nuova_data = st.text_input(
                  "Data (YYYY-MM-DD)", value=str(item["data"])
              )
              nuovo_totale = st.number_input(
                  "Totale (€)", value=float(item["totale"]), step=0.1
              )

            btn_salva, btn_elimina = st.columns([1, 1])
            with btn_salva:
              if st.form_submit_button(
                  "💾 Salva Modifiche", use_container_width=True
              ):
                aggiorna_movimento(
                    item_id, nuovo_negozio, nuova_data, nuovo_totale, nuovo_tipo
                )
                st.session_state[f"editing_{item_id}"] = False
                st.rerun()
            with btn_elimina:
              if st.form_submit_button(
                  "🗑️ Elimina Movimento", type="secondary", use_container_width=True
              ):
                elimina_movimento(item_id)
                st.session_state[f"editing_{item_id}"] = False
                st.rerun()

      st.markdown(
          "<hr style='margin-top:2px; margin-bottom:2px; border:0.2px solid"
          " #444;'>",
          unsafe_allow_html=True,
      )

  else:
    st.info("Nessun movimento registrato in Supabase.")

# TAB 5: RICONCILIAZIONE BANCARIA (CSV, EXCEL & PDF)
with tab5:
  st.subheader("🔍 Riconciliazione tra Estratto Conto Bancario e App")
  st.write(
      "Carica il file dell'estratto conto scaricato dalla tua banca (in"
      " formato **CSV**, **Excel `.xlsx`** o **PDF**)."
  )

  file_banca = st.file_uploader(
      "Carica File Estratto Conto", type=["csv", "xlsx", "pdf"]
  )
  tolleranza_giorni = st.slider(
      "Tolleranza Giorni Data (per la contabilizzazione bancaria)", 0, 7, 3
  )

  if file_banca is not None:
    df_banca = None

    try:
      # CASO 1: FILE PDF
      if file_banca.name.endswith(".pdf"):
        if st.button("🤖 Estrai Movimenti da PDF", type="primary"):
          with st.spinner("Estrazione ed elaborazione del PDF in corso..."):
            pdf_bytes = file_banca.read()
            api_key = st.secrets.get("GEMINI_API_KEY") or os.environ.get(
                "GEMINI_API_KEY"
            )
            movimenti_estratti = []

            # Tentativo 1: Chiamata API Gemini
            if api_key:
              try:
                client = genai.Client(api_key=api_key)
                part_pdf = types.Part.from_bytes(
                    data=pdf_bytes, mime_type="application/pdf"
                )
                config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=EstrattoContoPDFData,
                    temperature=0.1,
                )
                prompt_pdf = (
                    "Analizza questo estratto conto bancario PDF. Estrai tutti i"
                    " movimenti indicando: data (YYYY-MM-DD), descrizione e"
                    " importo assoluto."
                )

                modelli = [
                    "gemini-2.5-flash",
                    "gemini-2.0-flash",
                    "gemini-1.5-flash",
                ]
                for modello in modelli:
                  try:
                    response = client.models.generate_content(
                        model=modello,
                        contents=[part_pdf, prompt_pdf],
                        config=config,
                    )
                    dati_pdf = response.parsed
                    if dati_pdf and dati_pdf.movimenti:
                      movimenti_estratti = [
                          {
                              "data": m.data,
                              "descrizione": m.descrizione,
                              "importo": m.importo,
                          }
                          for m in dati_pdf.movimenti
                      ]
                      break
                  except Exception:
                    continue
              except Exception:
                pass

            # Tentativo 2: Fallback locale strutturato (pdfplumber + pypdf)
            if not movimenti_estratti:
              st.info(
                  "ℹ️ Quota API esaurita o risposta vuota. Utilizzo"
                  " dell'estrattore PDF locale (pdfplumber/pypdf)..."
              )
              movimenti_estratti = estrai_movimenti_pdf_locale(pdf_bytes)

            if movimenti_estratti:
              st.session_state["pdf_movimenti_banca"] = movimenti_estratti
              st.success(
                  f"Estratti {len(movimenti_estratti)} movimenti dal PDF!"
              )
            else:
              st.error(
                  "Impossibile estrarre movimenti dal PDF. Assicurati che il PDF"
                  " non sia un'immagine scansionata."
              )

        if "pdf_movimenti_banca" in st.session_state:
          df_banca = pd.DataFrame(st.session_state["pdf_movimenti_banca"])
          c_data, c_desc, c_importo = "data", "descrizione", "importo"

      # CASO 2: FILE CSV O EXCEL
      else:
        if file_banca.name.endswith(".csv"):
          try:
            df_banca = pd.read_csv(
                file_banca,
                sep=None,
                engine="python",
                on_bad_lines="skip",
                encoding="utf-8",
            )
          except Exception:
            file_banca.seek(0)
            df_banca = pd.read_csv(
                file_banca,
                sep=None,
                engine="python",
                on_bad_lines="skip",
                encoding="latin1",
            )
        else:
          df_banca = pd.read_excel(file_banca)

        cols = pd.Series(df_banca.columns)
        for dup in cols[cols.duplicated()].unique():
          cols[cols == dup] = [
              f"{dup}_{i}" if i != 0 else dup for i in range(sum(cols == dup))
          ]
        df_banca.columns = cols

        st.write(
            "📌 **Seleziona le colonne corrispondenti del tuo file bancario:**"
        )
        col_names = list(df_banca.columns)

        col_sel1, col_sel2, col_sel3 = st.columns(3)
        with col_sel1:
          c_data = st.selectbox("Colonna Data", col_names)
        with col_sel2:
          c_desc = st.selectbox("Colonna Descrizione/Causale", col_names)
        with col_sel3:
          c_importo = st.selectbox("Colonna Importo", col_names)

      # ESECUZIONE CONFRONTO
      if df_banca is not None:
        if file_banca.name.endswith(".pdf") or st.button(
            "⚡ Avvia Confronto Movimenti", type="primary"
        ):
          movimenti_db = carica_storico()

          if not movimenti_db:
            st.warning("Nessun movimento registrato nell'app da confrontare.")
          else:
            df_db = pd.DataFrame(movimenti_db)
            df_db["data_dt"] = pd.to_datetime(df_db["data"], errors="coerce")
            df_db["totale_abs"] = df_db["totale"].astype(float).abs()

            df_banca["data_dt"] = pd.to_datetime(
                df_banca[c_data], errors="coerce"
            )

            importo_clean = (
                df_banca[c_importo]
                .astype(str)
                .str.replace("€", "")
                .str.replace(" ", "")
                .str.replace(".", "")
                .str.replace(",", ".")
            )
            df_banca["totale_abs"] = (
                pd.to_numeric(importo_clean, errors="coerce").abs()
            )

            riconciliati = []
            matched_db_ids = set()
            matched_banca_idx = set()

            for b_idx, b_row in df_banca.iterrows():
              if pd.isna(b_row["data_dt"]) or pd.isna(b_row["totale_abs"]):
                continue

              for db_item in movimenti_db:
                db_id = db_item["id"]
                if db_id in matched_db_ids:
                  continue

                db_dt = pd.to_datetime(db_item["data"])
                db_totale = abs(float(db_item["totale"]))

                diff_giorni = abs((b_row["data_dt"] - db_dt).days)
                if (
                    abs(b_row["totale_abs"] - db_totale) < 0.01
                    and diff_giorni <= tolleranza_giorni
                ):
                  matched_db_ids.add(db_id)
                  matched_banca_idx.add(b_idx)
                  riconciliati.append({
                      "data_banca": b_row["data_dt"].strftime("%Y-%m-%d"),
                      "data_app": str(db_item["data"]),
                      "desc_banca": b_row[c_desc],
                      "desc_app": db_item["negozio"],
                      "importo": f"€ {db_totale:.2f}",
                  })
                  break

            soli_app_filtrati = [
                x for x in movimenti_db if x["id"] not in matched_db_ids
            ]
            soli_banca_filtrati = df_banca.iloc[
                [i for i in range(len(df_banca)) if i not in matched_banca_idx]
            ]

            # MOSTRA RISULTATI RICONCILIAZIONE
            st.divider()
            st.subheader("📊 Esito della Riconciliazione")

            r1, r2, r3 = st.columns(3)
            r1.metric("🟢 Riconciliati (In entrambi)", len(riconciliati))
            r2.metric("🟡 Presenti solo nell'App", len(soli_app_filtrati))
            r3.metric("🔴 Presenti solo in Banca", len(soli_banca_filtrati))

            sub_t1, sub_t2, sub_t3 = st.tabs([
                f"🟢 Riconciliati ({len(riconciliati)})",
                f"🟡 Solo App ({len(soli_app_filtrati)})",
                f"🔴 Solo Banca ({len(soli_banca_filtrati)})",
            ])

            with sub_t1:
              if riconciliati:
                st.dataframe(
                    pd.DataFrame(riconciliati), use_container_width=True
                )
              else:
                st.info("Nessuna corrispondenza trovata.")

            with sub_t2:
              if soli_app_filtrati:
                st.dataframe(
                    pd.DataFrame(soli_app_filtrati)[
                        ["data", "negozio", "tipo", "totale"]
                    ],
                    use_container_width=True,
                )
              else:
                st.success(
                    "Tutti i movimenti dell'app sono stati trovati nell'estratto"
                    " conto!"
                )

            with sub_t3:
              if not soli_banca_filtrati.empty:
                st.dataframe(
                    soli_banca_filtrati[[c_data, c_desc, c_importo]],
                    use_container_width=True,
                )
              else:
                st.success(
                    "Tutti i movimenti dell'estratto conto bancario sono"
                    " registrati nell'app!"
                )

    except Exception as e:
      st.error(f"Errore nella lettura o elaborazione del file bancario: {e}")
