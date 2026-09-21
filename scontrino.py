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
st.title("🧾 Gestione Entrate, Uscite e PDF (Supabase)")

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


# Schemi Pydantic per i dati strutturati da Gemini
class ScontrinoData(BaseModel):
  nome_negozio: str = Field(description="Nome dell'esercente")
  data: Optional[str] = Field(
      default=None,
      description=(
          "Data dello scontrino (YYYY-MM-DD) se ben visibile, altrimenti null"
      ),
  )
  totale_euro: float = Field(description="Importo totale finale pagato in Euro")


class VocaleData(BaseModel):
  negozio: str = Field(
      description="Descrizione della spesa, entrata o nome del negozio"
  )
  totale: float = Field(description="Importo in euro menzionato nell'audio")
  tipo: str = Field(
      description=(
          "Tipo di movimento: 'Entrata' (se si parla di guadagni, stipendi,"
          " incassi) o 'Uscita' (se si parla di spese, acquisti, pagamenti)"
      )
  )
  data: Optional[str] = Field(
      default=None,
      description=(
          "Data menzionata nel formato YYYY-MM-DD. Se si riferisce ad 'oggi',"
          " usa null"
      ),
  )


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
tab1, tab2, tab3, tab4 = st.tabs([
    "📷 Scansiona Scontrino (Uscita)",
    "🎤 Inserimento Vocale",
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

# TAB 2: INSERIMENTO VOCALE CON BOTTONE DI SALVATAGGIO
with tab2:
  st.subheader("Registra una nota vocale per la tua spesa o entrata")
  st.write(
      "Esempio: *'Ho speso 15 euro e 50 al bar per la colazione'* oppure *'Ho"
      " incassato 200 euro per una consulenza'*"
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
      with st.spinner("Ascolto e analisi in corso con Gemini..."):
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
            "Ascolta attentamente questo messaggio audio. Estrai la descrizione"
            " della spesa o entrata, l'importo totale in euro, se si tratta di"
            " un'Entrata o un'Uscita, e la data se menzionata espressamente."
        )

        modelli = ["gemini-3.6-flash", "gemini-3.5-flash"]
        dati_vocali = None
        ultimo_errore = None

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
          except Exception as e:
            ultimo_errore = e
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
          st.error(f"Errore durante l'analisi dell'audio: {ultimo_errore}")

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

      st.success("Movimento salvato con successo su Supabase!")

      st.session_state["dati_vocali_temp"] = None
      st.session_state["audio_key"] += 1

      time.sleep(1)
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
        st.success(
            f"Registrata {tipo_str} in Supabase: **{m_negozio}** - €"
            f" {m_totale:.2f} ({data_str})"
        )

# TAB 4: BILANCIO IN TABELLA RACCOLTA & EXPORT PDF
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

    # Intestazione della Tabella
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

    # Righe della Tabella
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

      # Form di modifica a comparsa direttamente sotto la riga selezionata
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
                st.success("Modificato!")
                st.rerun()
            with btn_elimina:
              if st.form_submit_button(
                  "🗑️ Elimina Movimento", type="secondary", use_container_width=True
              ):
                elimina_movimento(item_id)
                st.session_state[f"editing_{item_id}"] = False
                st.success("Eliminato!")
                st.rerun()

      st.markdown(
          "<hr style='margin-top:2px; margin-bottom:2px; border:0.2px solid"
          " #444;'>",
          unsafe_allow_html=True,
      )

  else:
    st.info("Nessun movimento registrato in Supabase.")
