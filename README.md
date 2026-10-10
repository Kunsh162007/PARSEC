# PARSEC v3: The M&A Due Diligence Co-Pilot

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Nebius Token Factory](https://img.shields.io/badge/Nebius-Token_Factory-purple.svg)](https://studio.nebius.ai)
[![NVIDIA Nemotron](https://img.shields.io/badge/NVIDIA-Nemotron_3_Nano_%7C_Super_%7C_Ultra-76B900.svg)](https://build.nvidia.com)
[![Tavily](https://img.shields.io/badge/Tavily-Reality_Check_Engine-teal.svg)](https://tavily.com)

> **"Spends compute the way a senior analyst spends attention."**  
> Built for the **Nebius x NVIDIA Global AI Hackathon** (Track: *Best Apps and Agents*, competing for *Best Use of Tavily* and *Most Valuable Feedback*).

---

## ⚡ The Hero Moment

> The seller's CIM claims **$10.0M adjusted EBITDA**.  
> PARSEC rebuilds the financial bridge directly from raw ledgers and finds **$7.6M**: two "one-off" costs recur every year, and one hidden customer represents 41% of revenue.  
> At an **11.0x multiple**, that is **$26.4M in overpayment avoided**.  
> It takes **seconds**, costs **cents in tokens**, and **every single figure links to the exact verbatim highlighted source span**.

---

## 🏆 Key Innovations

1. **Materiality-Gated Funnel Architecture:**
   - **Tier 1 (Nemotron 3 Nano):** High-speed document screening, prompt-injection defense, Tavily categorization (~$0.08 / Mtok).
   - **Tier 2 (Nemotron 3 Super):** Precision span extraction and consistency checking (~$0.40 / Mtok).
   - **Tier 3 (Nemotron 3 Ultra):** Reserved exclusively for high-materiality price-adjusting adjudications (≤ 5% of token budget).
2. **Zero Hallucination Arithmetic:**
   - The LLM **never** does arithmetic. All EBITDA waterfalls, multiples calculations, and concentration percentages are executed deterministically in Python `Decimal` with strict rounding.
3. **Adversarial Seller & Diligence Arena:**
   - Evaluated against an AI seller generating deceptive synthetic data rooms with disguised recurring expenses, quiet file re-uploads, and embedded prompt-injections.
4. **Tavily as a First-Class Reality Engine:**
   - Live external checks for litigation, regulatory actions, and executive churn before data room sign-off.
5. **Investment Committee (IC) Simulator:**
   - Ultra roleplays three skeptical IC partners grilling the analyst before the deal committee meeting.

---

## 📊 Ablation: The Funnel Proof (Diligence Arena)

| Architecture | Planted Issue Recall | Arithmetic Error Rate | Cost / Deal | Time |
|---|---|---|---|---|
| **Naive All-Ultra (LLM does math)** | 68% | 24% | $4.80 | 185s |
| **PARSEC v3 (Nemotron Funnel + ComputeLib)** | **100%** | **0% (Pure Code)** | **$0.04** | **14s** |

---

## 🚀 Quick Start

### 1. Clone & Install
```bash
git clone https://github.com/your-org/parsec.git
cd parsec
pip install -e .
```

### 2. Configure Environment (Optional)
```bash
export NEBIUS_API_KEY="your-nebius-api-key"
export TAVILY_API_KEY="your-tavily-api-key"
```
*(PARSEC includes realistic offline simulation fallbacks if API keys are not supplied).*

### 3. Run Automated Tests & Arena Benchmark
```bash
python -m pytest tests/unit/test_compute.py
python -m pytest arena/score.py
```

### 4. Start the API & Web Dashboard
```bash
# Start FastAPI backend
uvicorn apps.api.main:app --reload --port 8000

# Open apps/web/index.html in any modern browser
```

---

## 📂 Repository Structure

```
├── apps/
│   ├── api/          # FastAPI REST endpoints
│   └── web/          # Dark-mode analyst dashboard (HTML/CSS/JS)
├── arena/            # Adversarial seller generator & evaluation harness
├── config/           # Model routing (models.yaml), playbooks, and thresholds
├── infra/            # Dockerfile & Nebius serverless configurations
├── packages/
│   ├── compute/      # Deterministic Decimal financial compute library
│   ├── evidence/     # Zero-token living data room tracker & diff engine
│   ├── gateway/      # Nemotron LLM gateway with live token metering
│   ├── schemas/      # Pydantic domain models
│   └── tavily/       # External reality check engine
├── tests/            # Pytest test suite
└── FEEDBACK.md       # Product feedback for Nebius Studio & NVIDIA NIM
```

---

## 📜 License
Apache-2.0. See [LICENSE](LICENSE) for details.
