"""Post-deployment evaluation harness for the Card Transaction MCP server.

Each suite returns a SuiteResult written to evaluation/evidence/<suite>.json;
reporting.generate_reports() turns the evidence into JSON/CSV/Markdown/HTML
reports and the evaluation test matrix under evaluation/reports/.
"""
