# AML Compliance Agent

A complete **Anti-Money Laundering (AML) compliance** example for the OpenAI Agents SDK, demonstrating a multi-agent workflow for financial crime prevention.

## 🎥 Demo Video & Screenshots

### Demo Video (3 minutes)
[Watch the full demo](your-video-link-here)

### Screenshots

#### 1. Model Loading
![Loading](screenshot-01-start.png)
*Loading Gemma 2B model locally*

#### 2. Sanctions Screening
![Screening](screenshot-02-screening.png)
*Step 1: Checking sanctions lists*

#### 3. Risk Assessment
![Risk](screenshot-03-risk.png)
*Step 2: Evaluating customer risk*

#### 4. Transaction Monitoring
![Monitoring](screenshot-04-monitoring.png)
*Step 3: Analyzing transactions*

#### 5. Compliance Report
![Report](screenshot-05-report.png)
*Step 4: Generating final report*

#### 6. Complete (100% Offline)
![Complete](screenshot-06-complete.png)
*All checks completed offline*

## 🌟 Features

- **Multi-Agent Workflow**: Screening → Risk Assessment → Transaction Monitoring → Reporting
- **Structured Outputs**: Type-safe Pydantic models for all agent outputs
- **Real-World Patterns**: Implements actual AML compliance workflows
- **Guardrails**: Input/output validation for regulatory compliance
- **Offline Mode**: Local Gemma model support for privacy

## 📁 Architecture

```
aml_compliance_agent/
├── agents/
│   ├── screening_agent.py      # Sanctions/PEP screening
│   ├── risk_assessment_agent.py # Customer risk rating
│   ├── alert_agent.py          # Suspicious activity detection
│   └── report_agent.py         # Compliance report generation
├── tools/
│   ├── sanctions_checker.py    # Mock sanctions list checking
│   └── transaction_analyzer.py # Transaction pattern analysis
├── manager.py                  # Orchestrates the workflow
├── main.py                     # OpenAI API version
└── main_gemma.py              # Local Gemma version (offline)
```

## 🚀 Quick Start

### Option 1: OpenAI API (Cloud)

```bash
# Set your OpenAI API key
export OPENAI_API_KEY=your_key

# Run the demo
python -m examples.aml_compliance_agent.main
```

### Option 2: Local Gemma (Offline)

```bash
# Set HuggingFace token
export HF_TOKEN=your_huggingface_token

# Install dependencies
pip install transformers torch accelerate bitsandbytes

# Run offline version
python -m examples.aml_compliance_agent.main_gemma
```

## 📊 Demo Output

```
============================================================
AML Compliance Check (Local Gemma)
Customer: John Smith
ID: CUST-001
============================================================

[1/4] Running sanctions screening...
      Result: Risk Level: low
             Recommended Action: clear
             Details: No sanctions matches found...

[2/4] Performing risk assessment...
      Result: Overall Risk: low
             Review Frequency: annual
             Justification: Standard customer profile...

[3/4] Analyzing transactions...
      No alerts generated

[4/4] Generating compliance report...
      Report: Status: compliant
             Next Review: 2026-04-16
             Summary: Customer cleared all checks...

============================================================
Compliance Check Complete (100% Offline)
============================================================
```

## 🏗️ How It Works

### 1. Sanctions Screening
- Checks against OFAC, UN, EU sanctions lists
- Identifies Politically Exposed Persons (PEP)
- Searches for adverse media

### 2. Risk Assessment
- Evaluates geographic risk
- Assesses business activity risk
- Determines customer risk profile
- Sets review frequency

### 3. Transaction Monitoring
- Detects structuring (smurfing)
- Identifies rapid fund movement
- Flags high-risk jurisdictions
- Recognizes suspicious patterns

### 4. Compliance Reporting
- Generates structured reports
- Documents findings
- Recommends actions
- Sets next review date

## 💡 Use Cases

- **Banks**: Customer onboarding compliance checks
- **Fintechs**: Automated AML screening
- **Regulators**: Audit and examination support
- **Consultants**: Compliance program templates

## 🔒 Privacy Mode

The `main_gemma.py` version runs completely offline:
- ✅ No data sent to cloud
- ✅ Local Gemma 2B model
- ✅ Works in air-gapped environments
- ✅ No API costs

## 🛠️ Extending

### Add Real Sanctions API
```python
# Replace mock in sanctions_checker.py
def check_sanctions_list(name: str) -> dict:
    # Integrate with real OFAC API
    response = requests.get(f"https://api.ofac.gov/search?name={name}")
    return response.json()
```

### Add Database Storage
```python
# Store results in your compliance database
from .database import save_compliance_report
save_compliance_report(customer_id, report)
```

## 📚 Learn More

- [OpenAI Agents SDK Docs](https://github.com/openai/openai-agents-python)
- [AML Compliance Basics](https://www.fincen.gov/resources/aml)
- [Gemma Model](https://huggingface.co/google/gemma-2b-it)

## 🤝 Contributing

Contributions welcome! Areas for improvement:
- Real sanctions API integration
- Additional risk models
- More transaction patterns
- Multi-language support

## 📝 License

MIT License - See [LICENSE](../../LICENSE) for details

---

**Built with ❤️ for the OpenAI Agents SDK**
