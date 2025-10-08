# streamlit_app.py
# Remplace entièrement ton ancien fichier par celui-ci.
# Requirements: streamlit, PyPDF2
#
# Fonctionnalités :
# - multi-catégories (data/<categorie>.json)
# - upload PDF / coller texte -> génération Cloze ou Q/A heuristique
# - ajout / édition / suppression de cartes
# - révision continue : cartes tirées avec pondération selon ease_factor
# - import / export JSON
# - sauvegarde automatique

import streamlit as st
import json, os, io, uuid, random, datetime, math, re
from typing import List, Optional

# essaye PyPDF2 (nécessaire pour l'extraction PDF)
try:
    import PyPDF2
except Exception:
    PyPDF2 = None

# -------------------------
# Configuration & paths
# -------------------------
DATA_FOLDER = "data"
ROOT_LEGACY_FILE = "flashcards.json"
os.makedirs(DATA_FOLDER, exist_ok=True)

# -------------------------
# Dataclass / modèle Card
# -------------------------
class Card:
    def __init__(
        self,
        id: str,
        question: str,
        answer: str,
        created_at: Optional[str] = None,
        interval: int = 0,
        repetitions: int = 0,
        ease_factor: float = 2.5,
        due_date: Optional[str] = None,
        history: Optional[List[dict]] = None,
    ):
        self.id = id
        self.question = question
        self.answer = answer
        self.created_at = created_at or datetime.date.today().isoformat()
        self.interval = interval
        self.repetitions = repetitions
        self.ease_factor = ease_factor
        self.due_date = due_date or datetime.date.today().isoformat()
        self.history = history or []

    def to_dict(self):
        return {
            "id": self.id,
            "question": self.question,
            "answer": self.answer,
            "created_at": self.created_at,
            "interval": self.interval,
            "repetitions": self.repetitions,
            "ease_factor": self.ease_factor,
            "due_date": self.due_date,
            "history": self.history,
        }

# -------------------------
# Helpers : fichiers / catégories
# -------------------------
def get_available_categories() -> List[str]:
    files = [f[:-5] for f in os.listdir(DATA_FOLDER) if f.endswith(".json")]
    if not files:
        # si data vide mais legacy file présent, proposer migration
        if os.path.exists(ROOT_LEGACY_FILE):
            return ["default"]
        return []
    return sorted(files)

def get_storage_file(category: str) -> str:
    return os.path.join(DATA_FOLDER, f"{category}.json")

def migrate_legacy_to_default():
    """Si flashcards.json existe à la racine et no categories, le migrer."""
    if os.path.exists(ROOT_LEGACY_FILE) and not get_available_categories():
        try:
            with open(ROOT_LEGACY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            target = get_storage_file("default")
            with open(target, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False
    return False

def load_cards(category: str) -> List[Card]:
    file_path = get_storage_file(category)
    if not os.path.exists(file_path):
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cards = []
        for c in data:
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
        st.error(f"Erreur en lisant {file_path}: {e}")
        return []

def save_cards(cards: List[Card], category: str):
    file_path = get_storage_file(category)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump([c.to_dict() for c in cards], f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.error(f"Impossible d'écrire {file_path}: {e}")

# -------------------------
# Extraction PDF / génération automatique
# -------------------------
def extract_text_from_pdf(file_bytes) -> str:
    if PyPDF2 is None:
        st.error("PyPDF2 n'est pas installé : impossible d'extraire le PDF.")
        return ""
    text_parts = []
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        for page in reader.pages:
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
    except Exception as e:
        st.error(f"Erreur d'extraction PDF: {e}")
    return "\n".join(text_parts)

def split_into_sentences(text: str) -> List[str]:
    sentences = re.split(r'(?<=[\.\?\!;])\s+', text.replace("\n", " "))
    sentences = [s.strip() for s in sentences if len(s.strip()) > 15]
    return sentences

def generate_cloze_from_sentence(sentence: str) -> Optional[dict]:
    words = re.findall(r"\w+", sentence)
    if not words:
        return None
    candidates = [w for w in words if len(w) > 6]
    if not candidates:
        candidates = [w for w in words if len(w) > 4]
    if not candidates:
        return None
    blank = max(set(candidates), key=len)
    # Remplacer seulement la première occurrence
    pattern = re.compile(r"\b" + re.escape(blank) + r"\b", flags=re.IGNORECASE)
    question = pattern.sub("_____", sentence, count=1)
    if question == sentence:
        # fallback simple
        question = sentence.replace(blank, "_____", 1)
    return {"question": question, "answer": blank}

def generate_qa_from_text_by_lines(text: str) -> List[dict]:
    results = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if ":" in line and len(line.split(":")[0]) < 100:
            left, right = line.split(":", 1)
            question = left.strip()
            answer = right.strip()
            j = i + 1
            while j < min(i + 4, len(lines)) and not (":" in lines[j] and len(lines[j].split(":")[0]) < 100):
                if len(lines[j]) < 200:
                    answer += " " + lines[j]
                j += 1
            results.append({"question": question, "answer": answer})
    return results

def auto_generate_cards_from_text(text: str, max_cards: int = 80, method: str = "cloze") -> List[Card]:
    cards = []
    # Try Q/A by lines first if requested
    if method == "qa":
        qa_by_lines = generate_qa_from_text_by_lines(text)
        for qa in qa_by_lines:
            cards.append(Card(str(uuid.uuid4()), qa["question"], qa["answer"]))
            if len(cards) >= max_cards:
                return cards
    # Cloze generation from sentences
    sentences = split_into_sentences(text)
    for s in sentences:
        if method == "qa":
            # if QA requested but QA_by_lines didn't find enough, fall back to cloze
            pass
        cloze = generate_cloze_from_sentence(s)
        if cloze:
            q = cloze["question"]
            a = cloze["answer"]
            cards.append(Card(str(uuid.uuid4()), q, a))
            if len(cards) >= max_cards:
                break
    return cards

# -------------------------
# SM-2 (simplifié) + grading
# -------------------------
def sm2_update(card: Card, quality: int):
    # Map user quality 1..3 to SM2 0..5
    q_map = {1: 2, 2: 4, 3: 5}
    q = q_map.get(quality, 3)
    today = datetime.date.today()
    if q < 3:
        card.repetitions = 0
        card.interval = 1
    else:
        card.repetitions += 1
        if card.repetitions == 1:
            card.interval = 1
        elif card.repetitions == 2:
            card.interval = 6
        else:
            card.interval = math.ceil(card.interval * card.ease_factor)
    ef = card.ease_factor
    ef = ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    if ef < 1.3:
        ef = 1.3
    card.ease_factor = ef
    card.due_date = (today + datetime.timedelta(days=card.interval)).isoformat()
    card.history.append({"date": today.isoformat(), "q": q, "user_grade": quality})

def grade_card(card: Card, user_grade: int):
    """
    user_grade: 1 = Pas compris, 2 = Moyen, 3 = Bien
    On met à jour ease_factor + history via SM-2 simplifié,
    mais la sélection ignore due_date (révision continue).
    """
    sm2_update(card, user_grade)

# -------------------------
# Sélection pondérée (révision continue)
# -------------------------
def choose_next_card(cards: List[Card]) -> Optional[Card]:
    if not cards:
        return None
    weights = []
    for c in cards:
        # poids = inverse de la maîtrise : plus EF bas => plus souvent
        # on limite EF pour que poids reste positif
        # weight = max(0.2, 4.0 - c.ease_factor)
        weight = max(0.1, 4.0 - c.ease_factor)
        # on peut augmenter poids si peu d'historique
        if len(c.history) == 0:
            weight *= 1.2
        weights.append(weight)
    chosen = random.choices(cards, weights=weights, k=1)[0]
    return chosen

# -------------------------
# UI : Streamlit
# -------------------------
st.set_page_config(page_title="Flashcards - multi catégories", page_icon="🧠", layout="wide")
st.title("🧠 Flashcards — multi-catégories & révision continue")

# Si data vide mais un legacy existe, proposer migration
if not get_available_categories() and os.path.exists(ROOT_LEGACY_FILE):
    if st.sidebar.button("Migrer flashcards.json → data/default.json"):
        ok = migrate_legacy_to_default()
        if ok:
            st.success("Migration effectuée. Recharge la page.")
        else:
            st.error("Migration échouée. Vérifie le format de flashcards.json.")

# Sidebar : catégories / upload / import-export
st.sidebar.header("Catégories & import/export")
categories = get_available_categories()
if not categories:
    st.sidebar.info("Aucune catégorie trouvée. Crée-en une ci-dessous.")
    categories = []

selected_category = st.sidebar.selectbox("Choisis une catégorie", options=categories, index=0 if categories else -1)

with st.sidebar.expander("➕ Créer une nouvelle catégorie"):
    new_cat_name = st.text_input("Nom de la catégorie")
    if st.button("Créer la catégorie", key="create_cat"):
        if new_cat_name.strip():
            path = get_storage_file(new_cat_name.strip())
            if os.path.exists(path):
                st.warning("Cette catégorie existe déjà.")
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("[]")
                st.success(f"Catégorie '{new_cat_name.strip()}' créée. Recharge la page.")
        else:
            st.warning("Nom invalide.")

st.sidebar.markdown("---")
uploaded_file = st.sidebar.file_uploader("Uploader un PDF (ou .txt) pour générer des flashcards", type=["pdf", "txt"])
pasted_text = st.sidebar.text_area("Ou coller du texte ici (optionnel)", height=150)
generation_method = st.sidebar.selectbox("Méthode de génération automatique", ["Cloze (par phrases)", "Q/A heuristique par lignes (Term:Def)"])
max_generate = st.sidebar.number_input("Nombre max de cartes à générer", min_value=5, max_value=500, value=80, step=5)

if uploaded_file:
    bytes_data = uploaded_file.read()
    if uploaded_file.type == "application/pdf":
        extracted = extract_text_from_pdf(bytes_data)
    else:
        try:
            extracted = bytes_data.decode("utf-8")
        except Exception:
            extracted = ""
    st.sidebar.success("Fichier chargé. Utilise 'Générer' pour créer les cartes.")
    if st.sidebar.button("Générer des flashcards depuis le fichier"):
        if not selected_category:
            st.sidebar.error("Choisis d'abord une catégorie.")
        else:
            method = "cloze" if generation_method.startswith("Cloze") else "qa"
            new_cards = auto_generate_cards_from_text(extracted, max_cards=max_generate, method=method)
            cards = load_cards(selected_category)
            added = 0
            for nc in new_cards:
                cards.append(nc)
                added += 1
            save_cards(cards, selected_category)
            st.sidebar.success(f"{added} flashcards générées et ajoutées à '{selected_category}'.")

if pasted_text:
    if st.sidebar.button("Générer des flashcards depuis le texte collé"):
        if not selected_category:
            st.sidebar.error("Choisis d'abord une catégorie.")
        else:
            method = "cloze" if generation_method.startswith("Cloze") else "qa"
            new_cards = auto_generate_cards_from_text(pasted_text, max_cards=max_generate, method=method)
            cards = load_cards(selected_category)
            added = 0
            for nc in new_cards:
                cards.append(nc)
                added += 1
            save_cards(cards, selected_category)
            st.sidebar.success(f"{added} flashcards générées et ajoutées à '{selected_category}'.")

st.sidebar.markdown("---")
# Export current category
if selected_category:
    cards_for_export = load_cards(selected_category)
    if st.sidebar.button("Exporter cette catégorie (JSON)"):
        st.sidebar.success("Préparation de l'export...")
        json_str = json.dumps([c.to_dict() for c in cards_for_export], ensure_ascii=False, indent=2)
        st.sidebar.download_button("Télécharger JSON", data=json_str, file_name=f"{selected_category}_flashcards.json", mime="application/json")

# Import JSON file into selected category
uploaded_json = st.sidebar.file_uploader("Importer JSON de flashcards (.json)", type=["json"], key="import_json_sidebar")
if uploaded_json and st.sidebar.button("Importer JSON maintenant"):
    if not selected_category:
        st.sidebar.error("Sélectionne d'abord une catégorie.")
    else:
        try:
            data = json.load(uploaded_json)
            imported = 0
            cards = load_cards(selected_category)
            for c in data:
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
                        history=c.get("history", []),
                    )
                    cards.append(card)
                    imported += 1
            save_cards(cards, selected_category)
            st.sidebar.success(f"{imported} cartes importées dans '{selected_category}'.")
        except Exception as e:
            st.sidebar.error(f"Erreur lors de l'import: {e}")

st.sidebar.markdown("---")
st.sidebar.caption("Les fichiers de chaque catégorie sont stockés dans le dossier 'data/'.")

# -------------------------
# Main tabs
# -------------------------
tab1, tab2, tab3 = st.tabs(["Réviser", "Toutes les cartes", "Ajouter / Éditer"])

# Load current cards
cards = load_cards(selected_category) if selected_category else []

# --- Tab: Réviser ---
with tab1:
    st.header("Révision")
    if not selected_category:
        st.info("Crée ou sélectionne une catégorie dans la barre latérale.")
    else:
        if not cards:
            st.info("Aucune carte dans cette catégorie. Ajoute des cartes ou génère-en depuis un PDF.")
        else:
            # session management: conserver la carte courante jusqu'au grade
            if "current_card_id" not in st.session_state:
                st.session_state.current_card_id = None
            if "show_answer" not in st.session_state:
                st.session_state.show_answer = False

            # choix ou réutilisation de la carte courante
            current_card = None
            if st.session_state.current_card_id:
                current_card = next((c for c in cards if c.id == st.session_state.current_card_id), None)
            if current_card is None:
                current_card = choose_next_card(cards)
                if current_card:
                    st.session_state.current_card_id = current_card.id
                    st.session_state.show_answer = False

            if current_card:
                st.subheader(f"Catégorie : {selected_category} — cartes : {len(cards)}")
                st.markdown(f"### ❓ {current_card.question}")

                if not st.session_state.show_answer:
                    if st.button("👀 Montrer la réponse"):
                        st.session_state.show_answer = True
                        st.rerun()
                else:
                    st.info(current_card.answer)
                    st.markdown("**Évalue ta compréhension :**")
                    col1, col2, col3 = st.columns(3)
                    if col1.button("❌ Pas compris"):
                        grade_card(current_card, 1)
                        save_cards(cards, selected_category)
                        # préparer la prochaine carte
                        st.session_state.current_card_id = None
                        st.session_state.show_answer = False
                        st.rerun()
                    if col2.button("😐 Moyen"):
                        grade_card(current_card, 2)
                        save_cards(cards, selected_category)
                        st.session_state.current_card_id = None
                        st.session_state.show_answer = False
                        st.rerun()
                    if col3.button("✅ Compris"):
                        grade_card(current_card, 3)
                        save_cards(cards, selected_category)
                        st.session_state.current_card_id = None
                        st.session_state.show_answer = False
                        st.rerun()

                    st.markdown("---")
                    st.write(f"**Historique (dernier 6):**")
                    for h in current_card.history[-6:]:
                        st.write(f"- {h.get('date','?')} → SM2={h.get('q','?')}, note_utilisateur={h.get('user_grade','?')}")
                    st.write(f"EF: {current_card.ease_factor:.2f} | Répétitions: {current_card.repetitions} | Interval: {current_card.interval} jours")

# --- Tab: Toutes les cartes ---
with tab2:
    st.header("Toutes les cartes")
    if not selected_category:
        st.info("Crée ou sélectionne une catégorie.")
    else:
        st.write(f"Catégorie : **{selected_category}** — total cartes : {len(cards)}")
        q = st.text_input("Chercher (question / réponse)")
        view_mode = st.selectbox("Trier par", ["Question", "Création", "Ease factor"])
        filtered = cards
        if q:
            filtered = [c for c in cards if q.lower() in c.question.lower() or q.lower() in c.answer.lower()]
        if view_mode == "Question":
            filtered.sort(key=lambda x: x.question.lower())
        elif view_mode == "Création":
            filtered.sort(key=lambda x: x.created_at)
        else:
            filtered.sort(key=lambda x: x.ease_factor)

        for c in filtered:
            with st.expander(f"{c.question[:80]}"):
                st.write("**Réponse :**")
                st.write(c.answer)
                st.write(f"ID: {c.id}")
                st.write(f"EF: {c.ease_factor:.2f} | Rép: {c.repetitions} | Interval: {c.interval} j | Due: {c.due_date}")
                st.write("Historique:", c.history[-5:])
                col1, col2, col3 = st.columns([1,1,1])
                if col1.button("Éditer", key=f"edit_{c.id}"):
                    st.session_state.edit_id = c.id
                    st.experimental_rerun()
                if col2.button("Dupliquer", key=f"dup_{c.id}"):
                    newc = Card(str(uuid.uuid4()), c.question, c.answer)
                    cards.append(newc)
                    save_cards(cards, selected_category)
                    st.success("Carte dupliquée.")
                    st.experimental_rerun()
                if col3.button("Supprimer", key=f"del_{c.id}"):
                    cards = [cc for cc in cards if cc.id != c.id]
                    save_cards(cards, selected_category)
                    st.success("Carte supprimée.")
                    st.experimental_rerun()

# --- Tab: Ajouter / Éditer ---
with tab3:
    st.header("Ajouter une nouvelle carte")
    if not selected_category:
        st.info("Crée ou sélectionne une catégorie dans la barre latérale.")
    else:
        new_q = st.text_area("Question (nouvelle)", key="new_q")
        new_a = st.text_area("Réponse (nouvelle)", key="new_a")
        if st.button("Ajouter la carte"):
            if not new_q.strip() or not new_a.strip():
                st.error("Question et réponse ne doivent pas être vides.")
            else:
                nc = Card(str(uuid.uuid4()), new_q.strip(), new_a.strip())
                cards.append(nc)
                save_cards(cards, selected_category)
                st.success("Carte ajoutée.")
                st.experimental_rerun()

        # Edition d'une carte sélectionnée via session state
        edit_id = st.session_state.get("edit_id", None)
        if edit_id:
            card_to_edit = next((c for c in cards if c.id == edit_id), None)
            if card_to_edit:
                st.markdown("---")
                st.header("Édition de la carte")
                eq = st.text_area("Question (édition)", value=card_to_edit.question, key=f"eq_{card_to_edit.id}")
                ea = st.text_area("Réponse (édition)", value=card_to_edit.answer, key=f"ea_{card_to_edit.id}")
                edue = st.date_input("Date de prochaine révision (optionnel)", value=datetime.date.fromisoformat(card_to_edit.due_date) if card_to_edit.due_date else datetime.date.today(), key=f"edue_{card_to_edit.id}")
                if st.button("Enregistrer modifications"):
                    card_to_edit.question = eq
                    card_to_edit.answer = ea
                    card_to_edit.due_date = edue.isoformat()
                    save_cards(cards, selected_category)
                    st.success("Modifications enregistrées.")
                    st.session_state.edit_id = None
                    st.experimental_rerun()
                if st.button("Annuler édition"):
                    st.session_state.edit_id = None
                    st.experimental_rerun()
            else:
                st.warning("Carte introuvable pour l'édition.")
                st.session_state.edit_id = None

st.markdown("---")
st.caption("Les heuristiques de génération automatique sont simples — relis/édite les cartes générées. Pour une génération plus avancée (IA), on peut intégrer un modèle externe plus tard.")
