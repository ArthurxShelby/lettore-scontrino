from datetime import datetime
import io
import os
import time
from typing import Optional

from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel, Field
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
import streamlit as st
from supabase import Client, create_client

# Configurazione della pagina Streamlit
st.set_page_config(page_title="Gestione Bilancio & Scontrini", layout="centered")


# --- SISTEMA DI AUTENTICAZIONE CON PASSWORD ---
def verifica_password() -> bool:
  """Gestisce la schermata di login tramite password definita in st.secrets."""
  if "autenticato" not in st.session_state:
    st.session_state["autenticato"] = False

  if st.session_state["autenticato"]:
    return True

  st.title("🔒 Accesso Riservato")
  st.write("Inserisci la password per accedere al sistema di gestione bilancio.")

  # Recupera la password dai secrets o dalle variabili d'ambiente
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


# Controlla l'accesso prima di caricare il resto dell'applicazione
if not verifica_password():
  st.stop()


# --- INIZIO APPLICAZIONE (AUTENTICATA) ---
st.title("🧾 Gestione Entrate, Uscite e PDF (Supabase)")

# Pulsante per effettuare il Logout nella barra laterale
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


# Schema Pydantic per i dati di input da Gemini
class ScontrinoData(BaseModel):
  nome_negozio: str = Field(description="Nome dell'esercente")
  data: Optional[str] = Field(
      default=None,
      description=(
          "Data dello scontrino (YYYY-MM-DD) se ben visibile, altrimenti null"
      ),
  )
  totale_euro: float = Field(description="Importo totale finale pagato in Euro")


# --- FUNZIONI DI GESTIONE SUPABASE ---
def carica_storico() -> list:
  """Recupera tutti i movimenti da Supabase ordinati per data decrescente."""
  try:
    response = (
        supabase.table("movimenti")
        .select("*")
        .order("data", desc=True)
        .execute()
    )
    return response.data
  except Exception as e:
    st.error(f"Errore durante il caricamento dei dati da Supabase: {e}")
    return []


def salva_movimento(
    negozio: str, data: str, totale: float, tipo: str = "Uscita"
):
  """Inserisce un nuovo movimento nel database Supabase."""
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
  """Aggiorna un movimento esistente su Supabase."""
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
  """Elimina un movimento da Supabase."""
  try:
    supabase.table("movimenti").delete().eq("id", item_id).execute()
  except Exception as e:
    st.error(f"Errore durante l'eliminazione da Supabase: {e}")


# --- FUNZIONE GENERAZIONE PDF ---
def genera_pdf_storico(storico: list) -> bytes:
  """Crea un documento PDF formattato con saldo, entrate e uscite."""
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
tab1, tab2, tab3 = st.tabs([
    "📷 Scansiona Scontrino (Uscita)",
    "✍️ Inserimento Manuale",
    "📊 Bilancio & Export PDF",
])

# TAB 1: ACQUISIZIONE CON CAMERA
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

    if st.button("Analizza e Salva come Uscita", type="primary"):
      with st.spinner("Analisi in corso con Gemini..."):
        # Elenco modelli attivi da provare
        modelli = ["gemini-3.6-flash", "gemini-3.5-flash"]
        dati = None
        ultimo_errore = None

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
            "Analizza questo scontrino ed estrai nome negozio, data e totale"
            " finale in euro. Se la data non è visibile o è tagliata nella"
            " foto, lascia il campo data vuoto/null."
        )

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
          except Exception as e:
            ultimo_errore = e
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
          st.write(f"**Negozio:** {dati.nome_negozio}")
          st.write(f"**Data:** {data_finale}")
        else:
          st.error(
              f"Si è verificato un errore durante l'analisi: {ultimo_errore}"
          )

# TAB 2: INSERIMENTO MANUALE
with tab2:
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
        st.success(
            f"Registrata {tipo_str} in Supabase: **{m_negozio}** - €"
            f" {m_totale:.2f} ({data_str})"
        )

# TAB 3: BILANCIO IN TEMPO REALE, MODIFICA & PDF
with tab3:
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
    st.subheader("Elenco Movimenti (da Supabase)")

    for item in storico_attuale:
      item_id = item["id"]
      tipo = item.get("tipo", "Uscita")
      colore_ico = "🟢" if tipo == "Entrata" else "🔴"
      segno = "+" if tipo == "Entrata" else "-"

      col_info, col_modifica, col_elimina = st.columns([3, 1, 1])

      with col_info:
        st.write(
            f"📅 **{item['data']}** | {colore_ico} **{tipo}** | "
            f"🏪 **{item['negozio']}** | **{segno} € {float(item['totale']):.2f}**"
        )

      with col_modifica:
        if st.button("✏️ Modifica", key=f"edit_{item_id}"):
          st.session_state[f"editing_{item_id}"] = not st.session_state.get(
              f"editing_{item_id}", False
          )

      with col_elimina:
        if st.button("🗑️ Elimina", key=f"del_{item_id}", type="secondary"):
          elimina_movimento(item_id)
          st.success("Movimento eliminato con successo!")
          st.rerun()

      if st.session_state.get(f"editing_{item_id}", False):
        with st.form(key=f"form_edit_{item_id}"):
          nuovo_tipo = st.selectbox(
              "Tipo",
              ["Uscita", "Entrata"],
              index=0 if tipo == "Uscita" else 1,
          )
          nuovo_negozio = st.text_input(
              "Descrizione/Negozio", value=item["negozio"]
          )
          nuova_data = st.text_input(
              "Data (YYYY-MM-DD)", value=str(item["data"])
          )
          nuovo_totale = st.number_input(
              "Totale (€)", value=float(item["totale"]), step=0.1
          )

          if st.form_submit_button("💾 Salva Modifiche"):
            aggiorna_movimento(
                item_id, nuovo_negozio, nuova_data, nuovo_totale, nuovo_tipo
            )
            st.session_state[f"editing_{item_id}"] = False
            st.success("Modifiche salvate con successo!")
            st.rerun()

      st.divider()

    pdf_bytes = genera_pdf_storico(storico_attuale)
    st.download_button(
        label="📄 Scarica Report PDF Bilancio",
        data=pdf_bytes,
        file_name=f"report_bilancio_{datetime.now().strftime('%Y%m%d')}.pdf",
        mime="application/pdf",
        type="primary",
    )
  else:
    st.info("Nessun movimento registrato in Supabase.")
