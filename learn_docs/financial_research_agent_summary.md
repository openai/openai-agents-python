# Financial Research Agent - Complete Summary

## Overview

The **Financial Research Agent** is a sophisticated multi-agent system that demonstrates how to build complex AI workflows using the OpenAI Agents SDK. It orchestrates multiple specialized agents to produce comprehensive financial research reports.

**Location:** `examples/financial_research_agent/`

**Purpose:** Analyze companies and produce professional-grade financial research reports with executive summaries, risk analysis, and follow-up questions.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                    FINANCIAL RESEARCH MANAGER                        │
│                      (manager.py - Orchestrator)                     │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                    Orchestrates 5-Phase Pipeline
                                  │
        ┌─────────────┬───────────┼──────────┬─────────────┐
        │             │           │          │             │
        ▼             ▼           ▼          ▼             ▼
    ┌───────┐   ┌─────────┐  ┌────────┐  ┌────────┐  ┌──────────┐
    │PLANNER│   │ SEARCH  │  │WRITER  │  │SUB-    │  │VERIFIER  │
    │ AGENT │   │ AGENT   │  │ AGENT  │  │ANALYSTS│  │ AGENT    │
    └───────┘   └─────────┘  └────────┘  └────────┘  └──────────┘
        │             │           │          │             │
        │             │           │          ├─────────────┤
        │             │           │          │             │
        │             │           │      ┌────────┐   ┌──────┐
        │             │           │      │FINANCIALS  │RISK  │
        │             │           │      │ AGENT  │   │AGENT │
        │             │           │      └────────┘   └──────┘
        │             │           │          │             │
        │             │           └──────────┴─────────────┘
        │             │                (Called as Tools)
        │             │
        ▼             ▼
   Structured    Web Search
    Output         Tool
```

---

## File Structure

```
examples/financial_research_agent/
├── main.py                          # Entry point
├── manager.py                       # Orchestrator (FinancialResearchManager)
├── printer.py                       # UI/progress display
├── README.md                        # Documentation
└── agents/
    ├── planner_agent.py            # Phase 1: Generate search plan
    ├── search_agent.py             # Phase 2: Execute web searches
    ├── writer_agent.py             # Phase 3: Synthesize report
    ├── financials_agent.py         # Sub-analyst: Fundamentals
    ├── risk_agent.py               # Sub-analyst: Risk analysis
    └── verifier_agent.py           # Phase 4: Verify quality
```

---

## The 5-Phase Pipeline

### Phase 1: Planning 📋
**File:** `agents/planner_agent.py`

```python
planner_agent = Agent(
    name="FinancialPlannerAgent",
    instructions=PROMPT,
    model="o3-mini",
    output_type=FinancialSearchPlan,  # Structured output
)
```

**Purpose:** Convert user query into 5-15 targeted search terms

**Input:** User query (e.g., "Analyze Apple's Q4 earnings")

**Output:** `FinancialSearchPlan` containing:
```python
class FinancialSearchPlan(BaseModel):
    searches: list[FinancialSearchItem]
    # Each item has: reason (str) and query (str)
```

**Example Output:**
- "Apple Q4 2024 earnings report"
- "AAPL stock performance 2024"
- "Apple iPhone sales data"
- "Apple 10-K filing 2024"
- "Tim Cook earnings call transcript"

---

### Phase 2: Searching 🔍
**File:** `agents/search_agent.py`

```python
search_agent = Agent(
    name="FinancialSearchAgent",
    model="gpt-4.1",
    instructions=INSTRUCTIONS,
    tools=[WebSearchTool()],
    model_settings=ModelSettings(tool_choice="required"),
)
```

**Purpose:** Execute web searches and summarize results

**Key Features:**
- **Parallel Execution:** All searches run concurrently using `asyncio`
- **Tool Usage:** Uses built-in `WebSearchTool`
- **Concise Output:** Maximum 300 words per search
- **Focus:** Numbers, events, and quotes relevant to financial analysis

**Code Pattern:**
```python
# From manager.py:77-88
tasks = [asyncio.create_task(self._search(item))
         for item in search_plan.searches]

for task in asyncio.as_completed(tasks):
    result = await task
    results.append(result)
```

---

### Phase 3: Writing ✍️
**File:** `agents/writer_agent.py`

```python
writer_agent = Agent(
    name="FinancialWriterAgent",
    instructions=WRITER_PROMPT,
    model="gpt-4.1",
    output_type=FinancialReportData,
)
```

**Purpose:** Synthesize search results into comprehensive report

**Output Structure:**
```python
class FinancialReportData(BaseModel):
    short_summary: str              # 2-3 sentence executive summary
    markdown_report: str            # Full long-form report
    follow_up_questions: list[str]  # Suggested next research steps
```

**Special Feature - Agents as Tools:**

The writer has access to two sub-analyst agents as tools:

```python
# From manager.py:102-112
fundamentals_tool = financials_agent.as_tool(
    tool_name="fundamentals_analysis",
    tool_description="Get financial metrics analysis",
    custom_output_extractor=_summary_extractor,
)

risk_tool = risk_agent.as_tool(
    tool_name="risk_analysis",
    tool_description="Get risk assessment",
    custom_output_extractor=_summary_extractor,
)

writer_with_tools = writer_agent.clone(tools=[fundamentals_tool, risk_tool])
```

The writer can **optionally call** these sub-analysts during report creation.

---

### Phase 3a: Sub-Analysts (Optional) 🔬

#### Financials Agent
**File:** `agents/financials_agent.py`

```python
financials_agent = Agent(
    name="FundamentalsAnalystAgent",
    instructions=FINANCIALS_PROMPT,
    output_type=AnalysisSummary,
)
```

**Focus:** Revenue, profit, margins, growth trajectory
**Output:** Short analysis (under 2 paragraphs)

#### Risk Agent
**File:** `agents/risk_agent.py`

```python
risk_agent = Agent(
    name="RiskAnalystAgent",
    instructions=RISK_PROMPT,
    output_type=AnalysisSummary,
)
```

**Focus:** Red flags, competitive threats, regulatory issues, supply chain problems
**Output:** Short analysis (under 2 paragraphs)

Both return:
```python
class AnalysisSummary(BaseModel):
    summary: str  # Extracted by custom_output_extractor
```

---

### Phase 4: Verification ✅
**File:** `agents/verifier_agent.py`

```python
verifier_agent = Agent(
    name="VerificationAgent",
    instructions=VERIFIER_PROMPT,
    model="gpt-4o",
    output_type=VerificationResult,
)
```

**Purpose:** Audit the report for quality

**Checks:**
- Internal consistency
- Proper sourcing
- No unsupported claims
- Identifies gaps or issues

**Output:**
```python
class VerificationResult(BaseModel):
    verified: bool       # Is report coherent and plausible?
    issues: str          # Description of any problems
```

---

## Data Flow Example

```
User Input: "Write an analysis of Apple Inc.'s most recent quarter"
│
├─► PHASE 1: PLANNING
│   └─► Generates 10 search terms
│       ├─► "Apple Q4 earnings" ────┐
│       ├─► "AAPL stock 2024" ──────┤
│       ├─► "iPhone sales data" ─────┤──► [Parallel execution]
│       ├─► "Apple 10-K 2024" ───────┤
│       └─► ... 6 more searches ─────┘
│                                     │
├─► PHASE 2: SEARCHING               │
│   └─► 10 web searches executed ────┘
│       └─► 10 summaries (≤300 words each)
│
├─► PHASE 3: WRITING
│   ├─► Input: Original query + Search summaries
│   │
│   ├─► (Optional) Call fundamentals_analysis tool
│   │   └─► Returns: Analysis of revenue, margins, growth
│   │
│   ├─► (Optional) Call risk_analysis tool
│   │   └─► Returns: Analysis of threats and red flags
│   │
│   └─► Output: FinancialReportData
│       ├─► Executive Summary (2-3 sentences)
│       ├─► Full Markdown Report (multiple paragraphs)
│       └─► Follow-up Questions (list)
│
├─► PHASE 4: VERIFICATION
│   ├─► Input: Markdown report
│   └─► Output: VerificationResult
│       ├─► verified: true/false
│       └─► issues: "Description of any problems"
│
└─► USER OUTPUT
    ├─► Report Summary
    ├─► Full Report
    ├─► Follow-up Questions
    └─► Verification Results
```

---

## Key Design Patterns

### 1. **Orchestrator Pattern**
The `FinancialResearchManager` class orchestrates the entire workflow:
- Manages phase execution
- Handles data flow between agents
- Provides progress updates
- Maintains tracing

### 2. **Agents as Tools**
Sub-agents are converted to tools that other agents can call:

```python
tool = agent.as_tool(
    tool_name="custom_name",
    tool_description="What the tool does",
    custom_output_extractor=lambda result: result.final_output.summary
)
```

This allows the writer to **optionally consult** specialists without forced handoffs.

### 3. **Structured Outputs**
Every agent uses Pydantic models for type-safe, predictable outputs:
- Eliminates parsing errors
- Provides IDE autocomplete
- Enables validation
- Makes outputs programmatically usable

### 4. **Parallel Execution**
Search queries run concurrently using `asyncio`:
```python
tasks = [asyncio.create_task(func(item)) for item in items]
for task in asyncio.as_completed(tasks):
    result = await task
```

### 5. **Progressive Enhancement**
The system works with just search results, but can be enhanced with:
- Sub-analyst insights
- File search tools
- Custom data sources
- Additional verification steps

---

## Agent Configuration Summary

| Agent | Model | Purpose | Output Type | Tools |
|-------|-------|---------|-------------|-------|
| **Planner** | o3-mini | Generate search queries | `FinancialSearchPlan` | None |
| **Search** | gpt-4.1 | Execute searches | Text | `WebSearchTool` |
| **Financials** | (default) | Analyze fundamentals | `AnalysisSummary` | None |
| **Risk** | (default) | Identify risks | `AnalysisSummary` | None |
| **Writer** | gpt-4.1 | Synthesize report | `FinancialReportData` | Financials + Risk agents |
| **Verifier** | gpt-4o | Audit quality | `VerificationResult` | None |

---

## Running the Example

### Prerequisites
```bash
export OPENAI_API_KEY="your-key-here"
```

### Execution
```bash
python -m examples.financial_research_agent.main
```

### Example Queries
- "Write up an analysis of Apple Inc.'s most recent quarter."
- "Analyze Tesla's financial performance and competitive position."
- "Research Microsoft's cloud business growth trajectory."

### Output
The system displays:
1. **Progress updates** during execution
2. **Trace URL** for OpenAI platform debugging
3. **Report Summary** (executive summary)
4. **Full Report** (markdown formatted)
5. **Follow-up Questions** (for further research)
6. **Verification Results** (quality audit)

---

## Code Snippets

### Custom Output Extractor
```python
async def _summary_extractor(run_result: RunResult) -> str:
    """Extract just the summary field from AnalysisSummary."""
    return str(run_result.final_output.summary)
```

This extracts only the `summary` field from the structured output, so the tool call returns clean text instead of a full object.

### Streaming with Progress Updates
```python
result = Runner.run_streamed(writer_with_tools, input_data)
async for _ in result.stream_events():
    if time.time() - last_update > 5:
        self.printer.update_item("writing", update_messages[next_message])
```

Shows progressive status updates while the agent works.

### Error Handling
```python
try:
    result = await Runner.run(search_agent, input_data)
    return str(result.final_output)
except Exception:
    return None  # Gracefully handle failed searches
```

Individual search failures don't crash the entire pipeline.

---

## Key Learnings

### 1. **Sequential vs Parallel**
- **Sequential:** Planning → Searching → Writing → Verification
- **Parallel:** All searches execute concurrently

### 2. **Agent Composition**
Agents can be composed in multiple ways:
- **Handoffs:** Transfer control to another agent
- **Tools:** One agent calls another as a function
- **Sequential:** Output of one becomes input to next

### 3. **Structured Outputs**
Using Pydantic models ensures:
- Type safety
- Validation
- Predictable data structures
- Easy integration between agents

### 4. **Graceful Degradation**
The system continues even if:
- Some searches fail
- Sub-analysts aren't called
- Verification finds issues

### 5. **Observability**
Built-in tracing provides:
- Full execution visibility
- Debugging capabilities
- Performance monitoring
- Cost tracking

---

## Extending the Example

### Add File Search
```python
from agents import FileSearchTool

search_agent_enhanced = search_agent.clone(
    tools=[WebSearchTool(), FileSearchTool()]
)
```

### Add More Sub-Analysts
```python
# Create new specialized agents
valuation_agent = Agent(
    name="ValuationAgent",
    instructions="Analyze P/E ratios and valuation metrics",
    output_type=AnalysisSummary,
)

# Add to writer's tools
valuation_tool = valuation_agent.as_tool(
    tool_name="valuation_analysis",
    tool_description="Get valuation analysis",
    custom_output_extractor=_summary_extractor,
)
```

### Customize Report Structure
Modify `FinancialReportData`:
```python
class FinancialReportData(BaseModel):
    short_summary: str
    markdown_report: str
    follow_up_questions: list[str]
    confidence_score: float  # New field
    sources_cited: list[str]  # New field
```

---

## Comparison with Other Examples

### vs. `research_bot`
- **Financial Research:** More specialized (finance-focused)
- **Financial Research:** Has verification step
- **Financial Research:** Uses sub-analysts as tools
- **Research Bot:** More general-purpose

### vs. `customer_service`
- **Financial Research:** Sequential pipeline
- **Customer Service:** Dynamic handoffs between agents
- **Financial Research:** Produces structured reports
- **Customer Service:** Handles conversational interactions

---

## Best Practices Demonstrated

1. ✅ **Clear separation of concerns** - Each agent has one job
2. ✅ **Structured outputs** - Type-safe data flow
3. ✅ **Error handling** - Graceful degradation
4. ✅ **Parallel execution** - Performance optimization
5. ✅ **Progress tracking** - User feedback during long operations
6. ✅ **Verification step** - Quality assurance
7. ✅ **Tracing** - Debugging and monitoring
8. ✅ **Modular design** - Easy to extend and customize

---

## Summary

The Financial Research Agent demonstrates a **production-ready multi-agent system** that combines:
- Sequential orchestration
- Parallel execution
- Agent composition (as tools)
- Structured outputs
- Quality verification

It's an excellent template for building complex AI workflows that require multiple specialized agents working together to produce high-quality, reliable outputs.
