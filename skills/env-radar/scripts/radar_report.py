#!/usr/bin/env python3
"""radar_report.py — 报告渲染：markdown / json / html 输出格式。"""
import html
import json
import sys


class OutputFormatter:
    """统一输出格式化器，支持 markdown / json / html。"""

    def __init__(self, fmt: str = "markdown"):
        self.fmt = fmt

    def output(self, content: str, output_file: str | None = None) -> None:
        """输出内容到文件或 stdout。"""
        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"报告已写入: {output_file}", file=sys.stderr)
        else:
            print(content)

    def render_report(self, sections: list[dict], title: str) -> str:
        """根据格式渲染完整报告。"""
        if self.fmt == "json":
            return json.dumps(
                {"title": title, "sections": sections},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        elif self.fmt == "html":
            return self._render_html(sections, title)
        else:
            return self._render_markdown(sections, title)

    def _render_markdown(self, sections: list[dict], title: str) -> str:
        lines = [f"# {title}", ""]
        for sec in sections:
            lines.append(f"## {sec.get('title', '')}")
            lines.append("")
            if "text" in sec:
                lines.append(sec["text"])
            if "table" in sec:
                lines.append(self._markdown_table(sec["table"]))
            if "list" in sec:
                for item in sec["list"]:
                    lines.append(f"- {item}")
            if "tree" in sec:
                lines.append("```")
                lines.append(sec["tree"])
                lines.append("```")
            lines.append("")
        return "\n".join(lines)

    def _render_html(self, sections: list[dict], title: str) -> str:
        parts = [
            "<!DOCTYPE html>",
            "<html><head><meta charset='utf-8'>",
            f"<title>{html.escape(title)}</title>",
            "<style>",
            "body{font-family:sans-serif;max-width:900px;margin:2em auto;line-height:1.6;}",
            "h1{color:#0a4;}h2{color:#06a;margin-top:1.5em;}",
            "table{border-collapse:collapse;width:100%;margin:1em 0;}",
            "th,td{border:1px solid #ddd;padding:0.5em;text-align:left;}",
            "th{background:#f5f5f5;}code{background:#f0f0f0;padding:0.1em 0.3em;}",
            "</style></head><body>",
            f"<h1>{html.escape(title)}</h1>",
        ]
        for sec in sections:
            parts.append(f"<h2>{html.escape(sec.get('title', ''))}</h2>")
            if "text" in sec:
                parts.append(f"<p>{html.escape(sec['text'])}</p>")
            if "table" in sec:
                parts.append(self._html_table(sec["table"]))
            if "list" in sec:
                parts.append("<ul>")
                for item in sec["list"]:
                    parts.append(f"<li>{html.escape(str(item))}</li>")
                parts.append("</ul>")
        parts.append("</body></html>")
        return "\n".join(parts)

    @staticmethod
    def _markdown_table(table: dict) -> str:
        """将 {'headers': [...], 'rows': [[...]]} 转为 markdown 表格。"""
        headers = table["headers"]
        rows = table["rows"]
        if not headers:
            return ""
        lines = [
            "| " + " | ".join(str(h) for h in headers) + " |",
            "|" + "|".join("---" for _ in headers) + "|",
        ]
        for row in rows:
            lines.append("| " + " | ".join(str(c) for c in row) + " |")
        return "\n".join(lines)

    @staticmethod
    def _html_table(table: dict) -> str:
        headers = table["headers"]
        rows = table["rows"]
        parts = ["<table><thead><tr>"]
        for h in headers:
            parts.append(f"<th>{html.escape(str(h))}</th>")
        parts.append("</tr></thead><tbody>")
        for row in rows:
            parts.append("<tr>")
            for c in row:
                parts.append(f"<td>{html.escape(str(c))}</td>")
            parts.append("</tr>")
        parts.append("</tbody></table>")
        return "".join(parts)
