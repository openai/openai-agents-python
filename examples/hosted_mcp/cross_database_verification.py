import argparse
import asyncio

from agents import Agent, HostedMCPTool, ModelSettings, Runner, RunResult, RunResultStreaming

"""Cross-database CVE verification with a hosted MCP server (TensorFeed.ai).

This example demonstrates multi-tool composition through a single hosted MCP
server. The agent verifies a CVE across three independent vulnerability
databases (MITRE CVE List, CISA Known Exploited Vulnerabilities, FIRST.org
EPSS) by sequencing three tool calls on the same TensorFeed MCP server.

The premise: the actual production failure mode for security agents is not
hallucination but acting on a single source. Cross-source corroboration is
the fix, and one MCP server in the agent loop replaces N parallel API
integrations.

The TensorFeed MCP server exposes 17 free tools (no auth required) across
AI news, model pricing, AI service status, security advisories, SEC EDGAR
filings, FDA regulatory data, and US energy indicators. License: most
underlying data is US Government public domain or CC0; commercial
redistribution permitted; attribution preserved on every response.

Compared to the gitmcp single-server simple.py, this example shows how
multi-tool composition inside one server replaces what would otherwise be
multiple parallel HostedMCPTool entries plus reconciliation logic.
"""


async def main(verbose: bool, stream: bool, cve_id: str):
    question = (
        f"Verify {cve_id} across multiple databases. Call get_cve_record for the MITRE "
        f"record, get_kev_catalog to check whether the CVE appears in CISA's Known "
        f"Exploited Vulnerabilities catalog, and get_epss_score for the FIRST.org "
        f"exploitation likelihood. Then summarize: severity_band, exploited_in_wild "
        f"(true if KEV has the CVE), epss_probability, a confirmed_by list of which "
        f"databases returned data, and a one-sentence triage recommendation."
    )

    agent = Agent(
        name="Security Triage Assistant",
        instructions=(
            "You are a security analyst. For any CVE the user asks about, call the "
            "tensorfeed MCP server tools (get_cve_record + get_kev_catalog + "
            "get_epss_score) before answering. Always include a confirmed_by list of "
            "which databases returned data, and never speculate about CVEs that did "
            "not appear in at least one source."
        ),
        model_settings=ModelSettings(tool_choice="required"),
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "tensorfeed",
                    "server_url": "https://tensorfeed.ai/api/mcp",
                    "allowed_tools": [
                        "get_cve_record",
                        "get_kev_catalog",
                        "get_epss_score",
                    ],
                    "require_approval": "never",
                }
            )
        ],
    )

    run_result: RunResult | RunResultStreaming
    if stream:
        run_result = Runner.run_streamed(agent, question)
        async for event in run_result.stream_events():
            if event.type == "run_item_stream_event":
                print(f"Got event of type {event.item.__class__.__name__}")
        print(f"Done streaming; final result: {run_result.final_output}")
    else:
        run_result = await Runner.run(agent, question)
        print(run_result.final_output)

    if verbose:
        for item in run_result.new_items:
            print(item)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", default=False)
    parser.add_argument("--stream", action="store_true", default=False)
    parser.add_argument(
        "--cve",
        default="CVE-2024-3094",
        help="CVE id to verify (default: CVE-2024-3094, the XZ backdoor).",
    )
    args = parser.parse_args()

    asyncio.run(main(args.verbose, args.stream, args.cve))
