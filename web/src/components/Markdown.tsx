import type { ReactNode } from "react";

/** 行内格式：**加粗** 和 `代码` */
function inline(text: string, key: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) return <b key={`${key}${i}`}>{part.slice(2, -2)}</b>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={`${key}${i}`}>{part.slice(1, -1)}</code>;
    return part;
  });
}

const cells = (line: string) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

/** 够用的 Markdown 渲染：段落、标题、列表、表格、加粗、行内代码。不执行任何 HTML。 */
export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r/g, "").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const key = `b${i}`;
    if (!line.trim()) {
      i++;
    } else if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1] ?? "")) {
      const head = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push(
        <table key={key} className="md-table">
          <thead>
            <tr>{head.map((c, j) => <th key={j}>{inline(c, `${key}h${j}`)}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((r, k) => (
              <tr key={k}>{r.map((c, j) => <td key={j}>{inline(c, `${key}r${k}c${j}`)}</td>)}</tr>
            ))}
          </tbody>
        </table>,
      );
    } else if (/^\s*([-*•]|\d+[.、)])\s+/.test(line)) {
      const items: string[] = [];
      const ordered = /^\s*\d+[.、)]\s+/.test(line);
      while (i < lines.length && /^\s*([-*•]|\d+[.、)])\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*•]|\d+[.、)])\s+/, ""));
      const body = items.map((it, k) => <li key={k}>{inline(it, `${key}l${k}`)}</li>);
      out.push(ordered ? <ol key={key}>{body}</ol> : <ul key={key}>{body}</ul>);
    } else if (/^#{1,4}\s+/.test(line)) {
      out.push(<h5 key={key}>{inline(line.replace(/^#{1,4}\s+/, ""), key)}</h5>);
      i++;
    } else {
      out.push(<p key={key}>{inline(line, key)}</p>);
      i++;
    }
  }
  return <div className="md">{out}</div>;
}
