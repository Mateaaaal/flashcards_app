# streamlit_app.py
# Usage:
# 1) pip install -r requirements.txt
# 2) streamlit run streamlit_app.py
#
# Fonctionnalités :
# - Upload PDF / coller texte
# - Génération automatique (Q/A heuristique ou Cloze)
# - Ajout manuel / edition / suppression
# - Système de notation : Pas compris / Moyen / Bien compris
# - SM-2 simplifié pour réapparition pondérée
# - Sauvegarde automatique dans flashcards.json

import streamlit as st
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional
import json, uuid, datetime, math, os, io, re
import PyPDF2
import random

# ---------- Data model ----------
@dataclass
class Card:
    id: str
    question: str
    answer: str
    created_at: str
    interval: int = 0        # jours
    repetitions: int = 0
    ease_factor: float = 2.5
    due_date: str = None     # ISO date
    history: List[Dict] = field(default_factory=list)  # list of {date, grade}

    def to_dict(self):
        return asdict(self)

# ---------- Storage ----------
STORAGE_FILE = "flashcards.json"

def load_cards() -> List[Card]:
    if not os.path.exists(STORAGE_FILE):
        return []
    try:
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cards = []
        for c in data:
            # Backwards compat: ensure keys exist
            card = Card(
                id=c.get("id", str(uuid.uuid4())),
                question=c.get("question", ""),
                answer=c.get("answer", ""),
                created_at=c.get("created_at", datetime.date.today().isoformat()),
                interval=c.get("interval", 0),
                repetitions=c.get("repetitions", 0),
                ease_factor=c.get("ease_factor", 2.5),
                due_date=c.get("due_date", datetime.date.today().isoformat()),
                history=c.get("history", []),
            )
            cards.append(card)
        return cards
    except Exception as e:
        st.error(f"Erreur en lisant {STORAGE_FILE}: {e}")
        return []

def save_cards(cards: List[Card]):
    with open(STORAGE_FILE, "w", encoding="utf-8") as f:
        json.dump([c.to_dict() for c in cards], f, ensure_ascii=False, indent=2)

# ---------- PDF / Text extraction ----------
def extract_text_from_pdf(file_bytes) -> str:
    text = []
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        for page in reader.pages:
            page_text = page.extract_text() or ""
            text.append(page_text)
    except Exception as e:
        st.error(f"Impossible d'extraire le PDF: {e}")
    return "\n".join(text)

# ---------- Simple heuristics to generate flashcards ----------
def split_into_sentences(text: str) -> List[str]:
    # naive sentence splitter
    sentences = re.split(r'(?<=[\.\?\!])\s+', text.replace("\n", " "))
    sentences = [s.strip() for s in sentences if len(s.strip()) > 15]
    return sentences

def generate_cloze_from_sentence(sentence: str) -> Optional[Dict]:
    # choose a meaningful word to blank: longest word > 6 chars or a noun-like word (heuristic)
    words = re.findall(r"\w+", sentence)
    candidates = [w for w in words if len(w) > 6]
    if not candidates:
        candidates = [w for w in words if len(w) > 4]
    if not candidates:
        return None
    # pick most frequent candidate (or random)
    candidates = list(set(candidates))
    blank = max(candidates, key=len)
    question = re.sub(r"\b" + re.escape(blank) + r"\b", "_____", sentence, flags=re.IGNORECASE)
    if question == sentence:
        # fallback: replace a random word
        idx = random.randrange(len(words))
        blank = words[idx]
        question = sentence.replace(blank, "_____")
    return {"question": question, "answer": blank}

def generate_qa_from_text_by_lines(text: str) -> List[Dict]:
    # If text has lines like "Term: definition" or "Q: ... A: ..." try to parse
    results = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        # pattern "X: Y"
        if ":" in line and len(line.split(":")[0]) < 60 and len(line.split(":")[1]) > 0:
            left, right = line.split(":", 1)
            question = left.strip()
            answer = right.strip()
            # sometimes the "answer" continues on the next lines
            j = i+1
            while j < min(i+4, len(lines)) and not (":" in lines[j] and len(lines[j].split(":")[0]) < 60):
                # append short continuations
                if len(lines[j]) < 200:
                    answer += " " + lines[j]
                j += 1
            results.append({"question": question, "answer": answer})
    return results

def auto_generate_cards_from_text(text: str, max_cards: int = 50) -> List[Card]:
    cards = []
    # try Q/A by lines first
    qa_by_lines = generate_qa_from_text_by_lines(text)
    for qa in qa_by_lines:
        cards.append(Card(
            id=str(uuid.uuid4()),
            question=qa["question"],
            answer=qa["answer"],
            created_at=datetime.date.today().isoformat(),
            due_date=datetime.date.today().isoformat()
        ))
        if len(cards) >= max_cards:
            return cards

    # fallback: cloze generation from sentences
    sentences = split_into_sentences(text)
    for s in sentences:
        cloze = generate_cloze_from_sentence(s)
        if cloze:
            q = cloze["question"]
            a = cloze["answer"]
            cards.append(Card(
                id=str(uuid.uuid4()),
                question=q,
                answer=a,
                created_at=datetime.date.today().isoformat(),
                due_date=datetime.date.today().isoformat()
            ))
            if len(cards) >= max_cards:
                break
    return cards

# ---------- SM-2 algorithm (simplified) ----------
def sm2_update(card: Card, quality: int):
    """
    quality: 0..5 mapping:
      - we'll map user grades:
        Pas compris -> quality 2
        Moyen       -> quality 4
        Bien compris-> quality 5
    This is a simplified approach adapted to 1/2/3 buttons.
    """
    # Map quality (1..3) to SM-2 quality (0..5)
    # user: 1 -> Pas compris -> we treat as 2 (failed)
    #       2 -> Moyen -> 4 (acceptable)
    #       3 -> Bien -> 5 (perfect)
    q_map = {1: 2, 2: 4, 3: 5}
    q = q_map.get(quality, 3)
    today = datetime.date.today()
    if q < 3:
        # Failed: reset repetitions
        card.repetitions = 0
        card.interval = 1
    else:
        card.repetitions += 1
        if card.repetitions == 1:
            card.interval = 1
        elif card.repetitions == 2:
            card.interval = 6
        else:
            # increase interval by EF
            card.interval = math.ceil(card.interval * card.ease_factor)
    # update ease factor
    # EF': EF + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    ef = card.ease_factor
    ef = ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    if ef < 1.3:
        ef = 1.3
    card.ease_factor = ef
    # set due date
    next_due = today + datetime.timedelta(days=card.interval)
    card.due_date = next_due.isoformat()
    # record history
    card.history.append({"date": today.isoformat(), "q": q, "user_grade": quality})
    return card

# ---------- Utilities ----------
def cards_due(cards: List[Card]) -> List[Card]:
    today = datetime.date.today().isoformat()
    due = [c for c in cards if (c.due_date is None) or (c.due_date <= today)]
    # sort by due_date then created
    due.sort(key=lambda x: (x.due_date or today, x.created_at))
    return due

def find_card(cards: List[Card], card_id: str) -> Optional[Card]:
    for c in cards:
        if c.id == card_id:
            return c
    return None

# ---------- Streamlit UI ----------
st.set_page_config(page_title="Flashcards SRS", layout="centered")
st.title("🧠 Flashcards — Générateur + Système de répétition (SM-2 simplifié)")

# load
cards = load_cards()

# Sidebar: Import / Export / Upload
st.sidebar.header("Import / Export & Upload")
uploaded_file = st.sidebar.file_uploader("Uploader un PDF (ou fichier texte)", type=["pdf", "txt"])
pasted_text = st.sidebar.text_area("Ou coller du texte ici (optionnel)", height=150)

generate_method = st.sidebar.selectbox("Méthode de génération automatique", ["Cloze (par phrases)", "Q/A heuristique par lignes (Term: Def)"])
max_generate = st.sidebar.number_input("Nombre max de cartes à générer", min_value=5, max_value=500, value=80, step=5)

if uploaded_file:
    bytes_data = uploaded_file.read()
    if uploaded_file.type == "application/pdf":
        text = extract_text_from_pdf(bytes_data)
    else:
        try:
            text = bytes_data.decode("utf-8")
        except:
            text = ""
    st.sidebar.success("Fichier chargé.")
    if st.sidebar.button("Générer des flashcards depuis le PDF"):
        with st.spinner("Génération en cours…"):
            new_cards = auto_generate_cards_from_text(text, max_cards=max_generate)
            # If user selected the other method, try to convert
            if generate_method == "Q/A heuristique par lignes":
                # ensure Q/A method tried first (auto_generate already tries QA_by_lines first)
                pass
            # append to store and save
            added = 0
            for nc in new_cards:
                cards.append(nc)
                added += 1
            save_cards(cards)
            st.sidebar.success(f"{added} flashcards générées et ajoutées.")

if pasted_text and st.sidebar.button("Générer des flashcards depuis le texte collé"):
    with st.spinner("Génération en cours…"):
        new_cards = auto_generate_cards_from_text(pasted_text, max_cards=max_generate)
        added = 0
        for nc in new_cards:
            cards.append(nc)
            added += 1
        save_cards(cards)
        st.sidebar.success(f"{added} flashcards générées et ajoutées.")

# Export / Import JSON
st.sidebar.markdown("---")
if st.sidebar.button("Exporter toutes les flashcards (JSON)"):
    st.sidebar.success("Préparation de l'export...")
    json_str = json.dumps([c.to_dict() for c in cards], ensure_ascii=False, indent=2)
    st.sidebar.download_button("Télécharger JSON", data=json_str, file_name="flashcards_export.json", mime="application/json")

uploaded_json = st.sidebar.file_uploader("Importer JSON de flashcards (.json)", type=["json"], key="import_json")
if uploaded_json and st.sidebar.button("Importer JSON maintenant"):
    try:
        data = json.load(uploaded_json)
        imported = 0
        for c in data:
            # basic validation
            if "question" in c and "answer" in c:
                card = Card(
                    id=c.get("id", str(uuid.uuid4())),
                    question=c["question"],
                    answer=c["answer"],
                    created_at=c.get("created_at", datetime.date.today().isoformat()),
                    interval=c.get("interval", 0),
                    repetitions=c.get("repetitions", 0),
                    ease_factor=c.get("ease_factor", 2.5),
                    due_date=c.get("due_date", datetime.date.today().isoformat()),
                    history=c.get("history", [])
                )
                cards.append(card)
                imported += 1
        save_cards(cards)
        st.sidebar.success(f"{imported} cartes importées.")
    except Exception as e:
        st.sidebar.error(f"Erreur lors de l'import: {e}")

st.sidebar.markdown("---")
st.sidebar.markdown("⚙️ Sauvegarde automatique : `flashcards.json` (dans le dossier courant)")

# Main UI Tabs
tab1, tab2, tab3 = st.tabs(["Réviser (due)", "Toutes les cartes", "Ajouter / Éditer"])

# --- Tab: Réviser ---
with tab1:
    st.header("Révision")
    due = cards_due(cards)
    if not due:
        st.success("Aucune carte à réviser maintenant. Ajoute des cartes ou change la date de révision.")
    else:
        # show the first due card
        card = due[0]
        st.subheader("Question")
        st.write(card.question)
        if st.button("Montrer la réponse"):
            st.info(card.answer)
        st.markdown("**Évalue ta compréhension :**")
        col1, col2, col3 = st.columns(3)
        if col1.button("1 — Pas compris"):
            card = sm2_update(card, quality=1)
            save_cards(cards)
            st.rerun()
        if col2.button("2 — Moyen"):
            card = sm2_update(card, quality=2)
            save_cards(cards)
            st.rerun()
        if col3.button("3 — Bien compris"):
            card = sm2_update(card, quality=3)
            save_cards(cards)
            st.rerun()
        # show progress
        st.markdown("---")
        st.write(f"**Historique (dernier 5):**")
        for h in card.history[-5:]:
            st.write(f"- {h['date']} → quality SM2={h['q']}, note utilisateur={h['user_grade']}")
        st.write(f"Prochaine révision: {card.due_date} | Intervalle: {card.interval} jours | EF: {card.ease_factor:.2f}")

# --- Tab: Toutes les cartes ---
with tab2:
    st.header("Toutes les cartes")
    st.write(f"Total: {len(cards)}")
    query = st.text_input("Chercher (question / réponse)")
    view_mode = st.selectbox("Trier par", ["Due date", "Création", "Question"])
    filtered = cards
    if query:
        filtered = [c for c in cards if query.lower() in c.question.lower() or query.lower() in c.answer.lower()]
    if view_mode == "Due date":
        filtered.sort(key=lambda x: x.due_date or "")
    elif view_mode == "Création":
        filtered.sort(key=lambda x: x.created_at or "")
    else:
        filtered.sort(key=lambda x: x.question.lower())
    for c in filtered:
        with st.expander(f"{c.question[:80]}..."):
            st.write("**Réponse :**")
            st.write(c.answer)
            st.write(f"ID: {c.id}")
            st.write(f"Due: {c.due_date} | Interval: {c.interval} jours | EF: {c.ease_factor:.2f}")
            col1, col2, col3 = st.columns([1,1,1])
            if col1.button("Éditer", key=f"edit_{c.id}"):
                st.session_state["edit_id"] = c.id
            if col2.button("Dupliquer", key=f"dup_{c.id}"):
                newc = Card(
                    id=str(uuid.uuid4()),
                    question=c.question,
                    answer=c.answer,
                    created_at=datetime.date.today().isoformat(),
                    due_date=datetime.date.today().isoformat()
                )
                cards.append(newc)
                save_cards(cards)
                st.success("Carte dupliquée.")
                st.rerun()
            if col3.button("Supprimer", key=f"del_{c.id}"):
                cards = [cc for cc in cards if cc.id != c.id]
                save_cards(cards)
                st.success("Carte supprimée.")
                st.rerun()

# --- Tab: Ajouter / Éditer ---
with tab3:
    st.header("Ajouter une nouvelle carte")
    q_in = st.text_area("Question", key="new_q")
    a_in = st.text_area("Réponse", key="new_a")
    if st.button("Ajouter la carte"):
        if not q_in.strip() or not a_in.strip():
            st.error("Question et réponse ne doivent pas être vides.")
        else:
            nc = Card(
                id=str(uuid.uuid4()),
                question=q_in.strip(),
                answer=a_in.strip(),
                created_at=datetime.date.today().isoformat(),
                due_date=datetime.date.today().isoformat()
            )
            cards.append(nc)
            save_cards(cards)
            st.success("Carte ajoutée.")
            st.rerun()

    # Edition
    edit_id = st.session_state.get("edit_id", None)
    if edit_id:
        c = find_card(cards, edit_id)
        if c:
            st.markdown("---")
            st.header("Éditer la carte")
            new_q = st.text_area("Question (édition)", value=c.question, key=f"edit_q_{c.id}")
            new_a = st.text_area("Réponse (édition)", value=c.answer, key=f"edit_a_{c.id}")
            new_due = st.date_input("Date de prochaine révision", value=datetime.date.fromisoformat(c.due_date) if c.due_date else datetime.date.today(), key=f"edit_due_{c.id}")
            if st.button("Enregistrer les modifications"):
                c.question = new_q
                c.answer = new_a
                c.due_date = new_due.isoformat()
                save_cards(cards)
                st.success("Modifications enregistrées.")
                # clear edit_id
                st.session_state["edit_id"] = None
                st.rerun()
            if st.button("Annuler édition"):
                st.session_state["edit_id"] = None
                st.rerun()
        else:
            st.warning("Carte introuvable pour édition.")

# Save at the end (auto)
save_cards(cards)

st.markdown("---")
st.caption("Développé pour un usage local. Les heuristiques de génération automatique sont simples : relis et corrige les cartes générées avant de réviser. Pour des générations plus avancées (Q/A sémantique), on peut intégrer un modèle NLP plus puissant.") 
