import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import katex from 'katex'
import 'katex/dist/katex.min.css'
import { Check, Code2, Copy, Maximize2, ZoomIn, ZoomOut } from 'lucide-react'
import { toast } from 'sonner'
import { useTheme } from '@/lib/theme'
import { wikiImageUrl } from '@/api/wiki'

/** Guard for [text](url) links: only render a real anchor for http(s) or
 *  scheme-less (relative) URLs. Anything with another scheme (javascript:,
 *  data:, vbscript:, …) is treated as literal text, never an anchor.
 *
 *  Browsers strip leading/embedded control chars & whitespace from a URL before
 *  resolving it, so ` javascript:x` and `java\tscript:x` both execute. We mirror
 *  that: collapse ALL whitespace/control chars, then only permit an explicit
 *  `scheme:` when it is http(s). The caller also trims the raw URL for the href. */
const isSafeUrl = (u: string) => {
  const s = u.replace(/[\u0000-\u0020\u007f-\u009f]/g, '')
  const scheme = /^[a-z][a-z0-9+.-]*:/i.exec(s)
  return scheme ? /^https?:/i.test(scheme[0]) : true
}

/**
 * Minimal inline Markdown renderer. Tokens (in match-priority order):
 *   [[wikilink|alias]] · \( math \) · **bold** · ~~strike~~ · `code` ·
 *   [text](url) · *italic* / _italic_.
 * ORDER IS LOAD-BEARING inside the alternation: wikilink must precede the link
 * pattern (so `[[x]]` is not parsed as a link), and bold must precede italic
 * (so `**x**` is not parsed as `*`-italic-`*`). Alternatives use bounded `[^…]+`
 * classes or a single non-greedy `.+?` (bold/math) — no nested quantifiers — so
 * there is no catastrophic backtracking. The inner text of bold/italic/strike
 * and a link's LABEL are re-run through inline() so nested markup resolves; the
 * recursed slice is always strictly shorter, which bounds the recursion (the
 * wikilink target is NOT recursed). Single `*`/`_` obey a minimal left/right
 * flanking rule so intraword runs (`a_b_c`, `2*3*4`) stay literal.
 */
function inline(text: string, onWikiLink?: (target: string) => void): React.ReactNode[] {
  const parts: React.ReactNode[] = []
  // Bold is non-greedy (`.+?`) so it tolerates an inner opposite-emphasis
  // (`**a *b* c**`); italic keeps a bounded `[^*]+`/`[^_]+` class.
  const re = /(\[\[[^\]]+\]\]|\\\([\s\S]+?\\\)|\*\*.+?\*\*|~~[^~]+~~|`[^`]+`|\[[^\]]+\]\([^)]+\)|\*[^*]+\*|_[^_]+_)/g
  let last = 0, m: RegExpExecArray | null, k = 0
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith('[[')) {
      // Obsidian/wiki alias form: [[target|alias]]. Split on the FIRST `|` —
      // the part before is the nav TARGET, the part after (if any) is the
      // DISPLAY label. Multiple `|` keep the rest in the label; an empty/absent
      // alias falls back to the target so we never render a blank link.
      const inner = tok.slice(2, -2)
      const pipe = inner.indexOf('|')
      const target = (pipe === -1 ? inner : inner.slice(0, pipe)).trim()
      const display = (pipe === -1 ? '' : inner.slice(pipe + 1)).trim() || target
      // Only clickable when a handler is wired — never imply navigation that
      // does nothing (the previous bug: cursor-pointer with no onClick).
      parts.push(
        onWikiLink ? (
          <button
            key={k++}
            type="button"
            onClick={() => onWikiLink(target)}
            className="text-accent-brand bg-accent-brand/10 rounded px-1 py-px cursor-pointer hover:bg-accent-brand/20 transition-colors"
          >
            {display}
          </button>
        ) : (
          <span key={k++} className="text-accent-brand bg-accent-brand/10 rounded px-1 py-px transition-colors">
            {display}
          </span>
        ),
      )
    } else if (tok.startsWith('\\(')) {
      // Inline math \( … \) via KaTeX; on a KaTeX error fall back to a code chip.
      const tex = tok.slice(2, -2)
      let html = ''
      try {
        html = katex.renderToString(tex, { displayMode: false, throwOnError: false })
      } catch {
        html = ''
      }
      parts.push(
        html ? (
          <span key={k++} className="text-foreground" dangerouslySetInnerHTML={{ __html: html }} />
        ) : (
          <code key={k++} className="font-mono2 text-[12px] bg-muted rounded px-1 py-px">{tex}</code>
        ),
      )
    } else if (tok.startsWith('**')) {
      // Recurse so nested markup (e.g. `**[docs](url)**`) resolves. slice is strictly shorter.
      parts.push(<strong key={k++} className="font-semibold text-foreground">{inline(tok.slice(2, -2), onWikiLink)}</strong>)
    } else if (tok.startsWith('~~')) {
      parts.push(<del key={k++} className="line-through">{inline(tok.slice(2, -2), onWikiLink)}</del>)
    } else if (tok.startsWith('`')) {
      parts.push(<code key={k++} className="font-mono2 text-[12px] bg-muted rounded px-1 py-px">{tok.slice(1, -1)}</code>)
    } else if (tok.startsWith('[')) {
      // Inline link [text](url). `[[…]]` was already consumed above, so any
      // `[` reaching here is a genuine link. Only emit an anchor when the URL
      // passes the scheme guard; otherwise render the raw token as literal text.
      const lm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok)
      const label = lm ? lm[1] : tok
      // Trim the captured URL BEFORE the guard so ` javascript:x` can't slip
      // past the scheme test (browsers trim & would run it). Same trimmed
      // value is used for href. The label is recursed so `[**x**](url)` works.
      const url = lm ? lm[2].trim() : ''
      parts.push(
        lm && isSafeUrl(url) ? (
          <a
            key={k++}
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-accent-brand hover:underline"
          >
            {inline(label, onWikiLink)}
          </a>
        ) : (
          <span key={k++}>{tok}</span>
        ),
      )
    } else {
      // *italic* / _italic_ (bold `**…**` was consumed above). Enforce a
      // minimal CommonMark-ish flanking rule so intraword runs stay literal:
      // the delimiter may only OPEN when the preceding char isn't a word char
      // (and the run doesn't start with whitespace), and only CLOSE when the
      // following char isn't a word char (and the run doesn't end with
      // whitespace). This keeps `a_b_c` / `2*3*4` as plain text.
      const before = m.index > 0 ? text[m.index - 1] : ''
      const after = text[m.index + tok.length] ?? ''
      const innerEm = tok.slice(1, -1)
      const openable = !/\w/.test(before) && !/^\s/.test(innerEm)
      const closable = !/\w/.test(after) && !/\s$/.test(innerEm)
      if (!openable || !closable) {
        // Not a real emphasis run: emit the opening delimiter literally and
        // resume scanning right after it (inner text may still hold tokens).
        parts.push(tok[0])
        last = m.index + 1
        re.lastIndex = m.index + 1
        continue
      }
      parts.push(<em key={k++} className="italic">{inline(innerEm, onWikiLink)}</em>)
    }
    last = m.index + tok.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

/** A fenced code block (non-mermaid): tokenized, horizontally scrollable. */
function CodeBox({ code, lang }: { code: string; lang?: string }) {
  return (
    <div className="my-3 overflow-hidden rounded-apple-md border border-[hsl(var(--glass-border))] bg-muted/50">
      {lang && (
        <div className="px-3.5 pt-2 text-[10.5px] uppercase tracking-wide text-muted-foreground">{lang}</div>
      )}
      <pre className="overflow-x-auto px-3.5 py-3 text-[12.5px] leading-relaxed">
        <code className="font-mono2 whitespace-pre text-foreground">{code}</code>
      </pre>
    </div>
  )
}

/** An extracted wiki image: `![alt](path)` where `path` is resolved against the
 *  wiki via `wikiImageUrl`. Falls back to a clickable link on load error so a
 *  broken/removed image never renders as a dead box. */
function WikiImage({ alt, src, kb }: { alt: string; src: string; kb: string }) {
  const [failed, setFailed] = useState(false)
  const url = useMemo(() => wikiImageUrl(kb, src), [kb, src])
  if (failed) {
    return (
      <div className="my-3">
        <a href={url} target="_blank" rel="noopener noreferrer" className="text-accent-brand hover:underline text-[13px]">
          {alt ? `${alt} (${src})` : src}
        </a>
      </div>
    )
  }
  return (
    <div className="my-3">
      <img
        src={url}
        alt={alt}
        loading="lazy"
        onError={() => setFailed(true)}
        className="max-w-full h-auto rounded-apple-md border border-[hsl(var(--glass-border))]"
      />
    </div>
  )
}

/** Display (block) math via KaTeX. On a KaTeX error, falls back to the raw TeX
 *  in a code box so a malformed formula never crashes the message. */
function MathBlock({ tex }: { tex: string }) {
  let html = ''
  try {
    html = katex.renderToString(tex, { displayMode: true, throwOnError: false })
  } catch {
    return <CodeBox code={tex} />
  }
  return (
    <div
      className="my-3 overflow-x-auto text-center text-foreground"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}

/**
 * Render a ```mermaid block as an SVG diagram. Mermaid is lazily imported (its
 * own bundle chunk, loaded only when a diagram appears). Theme-aware; on any
 * import/parse/render error, falls back to the raw source in a code box so a
 * malformed diagram never crashes the message.
 *
 * Toolbar: toggle between the rendered diagram and the raw source, copy the
 * mermaid code to the clipboard, and zoom in/out the diagram (useful when the
 * diagram is taller/wider than the viewport — e.g. a long flow or ER chart).
 * Zoom applies a CSS scale on the SVG inside a scrollable frame so the diagram
 * can be inspected at a readable size without clipping.
 */
function MermaidBlock({ code }: { code: string }) {
  const { resolved } = useTheme()
  const ref = useRef<HTMLDivElement>(null)
  const [error, setError] = useState(false)
  const [showCode, setShowCode] = useState(false)
  const [copied, setCopied] = useState(false)
  const [scale, setScale] = useState(1)
  const id = 'mmd-' + useId().replace(/[^a-zA-Z0-9]/g, '')

  useEffect(() => {
    let cancelled = false
    setError(false)
    import('mermaid')
      .then(async ({ default: mermaid }) => {
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: resolved === 'dark' ? 'dark' : 'default',
        })
        const { svg } = await mermaid.render(id, code)
        if (!cancelled && ref.current) ref.current.innerHTML = svg
      })
      .catch(() => {
        if (!cancelled) setError(true)
      })
    return () => {
      cancelled = true
    }
  }, [code, resolved, id])

  // Reset zoom when the source changes (a new diagram starts at 100%).
  useEffect(() => setScale(1), [code])

  const copyCode = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      toast.success('Copied mermaid source')
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error('Copy failed')
    }
  }, [code])

  // Toolbar button base style.
  const btn =
    'inline-flex items-center gap-1.5 h-7 px-2 rounded-md text-[11.5px] font-medium text-muted-foreground hover:text-foreground hover:bg-accent transition-colors disabled:opacity-50'

  if (error) return <CodeBox code={code} lang="mermaid" />
  return (
    <div className="my-3">
      <div className="flex items-center gap-1 rounded-t-lg border border-b-0 border-[hsl(var(--glass-border))] bg-muted/40 px-1.5 py-1">
        <span className="mr-1 text-[10.5px] uppercase tracking-wide text-muted-foreground/70">mermaid</span>
        <span className="flex-1" />
        {!showCode && (
          <>
            <button
              type="button"
              className={btn}
              onClick={() => setScale((s) => Math.max(0.5, Math.round((s - 0.25) * 100) / 100))}
              title="Zoom out"
              aria-label="Zoom out"
            >
              <ZoomOut className="w-3.5 h-3.5" />
            </button>
            <button
              type="button"
              className={btn}
              onClick={() => setScale((s) => Math.min(3, Math.round((s + 0.25) * 100) / 100))}
              title="Zoom in"
              aria-label="Zoom in"
            >
              <ZoomIn className="w-3.5 h-3.5" />
            </button>
            <button
              type="button"
              className={btn}
              onClick={() => setScale(1)}
              title="Reset zoom"
              aria-label="Reset zoom"
              disabled={scale === 1}
            >
              <Maximize2 className="w-3.5 h-3.5" />
            </button>
          </>
        )}
        <button
          type="button"
          className={btn}
          onClick={copyCode}
          title="Copy mermaid source"
          aria-label="Copy mermaid source"
        >
          {copied ? <Check className="w-3.5 h-3.5 text-emerald-500" /> : <Copy className="w-3.5 h-3.5" />}
          {copied ? 'Copied' : 'Copy'}
        </button>
        <button
          type="button"
          className={btn}
          onClick={() => setShowCode((v) => !v)}
          title={showCode ? 'Show diagram' : 'Show source'}
          aria-label={showCode ? 'Show diagram' : 'Show source'}
        >
          <Code2 className="w-3.5 h-3.5" />
          {showCode ? 'Diagram' : 'Source'}
        </button>
      </div>

      {showCode ? (
        <CodeBox code={code} lang="mermaid" />
      ) : (
        <div
          className="rounded-b-lg border border-[hsl(var(--glass-border))] bg-[hsl(var(--glass-2))] overflow-auto"
          style={{ maxHeight: 560 }}
        >
          <div style={{ transform: `scale(${scale})`, transformOrigin: 'top left', width: scale > 1 ? `${100 / scale}%` : 'auto' }}>
            <div ref={ref} className="flex justify-center p-3 [&_svg]:h-auto [&_svg]:max-w-none" />
          </div>
        </div>
      )}
    </div>
  )
}

export default function MarkdownView({
  source,
  onWikiLink,
  kb,
}: {
  source: string
  /** Navigate to a `[[target]]` wikilink's page. Omit to render plain,
   *  non-interactive tokens (no `cursor-pointer` implying a dead click). */
  onWikiLink?: (target: string) => void
  /** Knowledge-base name, needed to resolve `![alt](path)` wiki images. When
   *  omitted, image markdown renders as a plain link instead of an `<img>`. */
  kb?: string
}) {
  const lines = source.split('\n')
  const out: React.ReactNode[] = []
  let list: string[] = []
  let olist: string[] = []
  // Source number of the first `<ol>` item, so a list starting at N≠1 keeps it.
  let olistStart = 1
  let key = 0

  const flushList = () => {
    if (!list.length) return
    out.push(
      <ul key={key++} className="my-2.5 space-y-1.5 pl-1">
        {list.map((li, i) => (
          <li key={i} className="flex gap-2 text-[14px] leading-relaxed text-muted-foreground">
            <span className="mt-[9px] w-1 h-1 rounded-full bg-muted-foreground shrink-0" />
            <span>{inline(li, onWikiLink)}</span>
          </li>
        ))}
      </ul>,
    )
    list = []
  }

  const flushOList = () => {
    if (!olist.length) return
    out.push(
      <ol key={key++} start={olistStart} className="my-2.5 space-y-1.5 pl-6 list-decimal">
        {olist.map((li, i) => (
          <li key={i} className="pl-1 text-[14px] leading-relaxed text-muted-foreground marker:text-muted-foreground">
            <span>{inline(li, onWikiLink)}</span>
          </li>
        ))}
      </ol>,
    )
    olist = []
    olistStart = 1
  }

  // Block boundary: any non-list line closes BOTH pending lists.
  const flushBlocks = () => { flushList(); flushOList() }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trimEnd()

    // Fenced code block: ``` or ```lang … ``` (accumulate until the closing fence)
    const fence = /^```(\w*)\s*$/.exec(line.trim())
    if (fence) {
      flushBlocks()
      const lang = fence[1]
      const body: string[] = []
      i++
      while (i < lines.length && !/^```\s*$/.test(lines[i].trim())) {
        body.push(lines[i])
        i++
      }
      // i now sits on the closing fence (or past the end if unterminated).
      const code = body.join('\n')
      if (lang === 'mermaid') out.push(<MermaidBlock key={key++} code={code} />)
      else out.push(<CodeBox key={key++} code={code} lang={lang || undefined} />)
      continue
    }

    // Block image: `![alt](path)` on its own line. Path is the wiki image ref
    // (note-relative or wiki-root-relative) resolved via the wiki endpoint.
    const img = /^!\[([^\]]*)\]\(([^)]+)\)\s*$/.exec(line.trim())
    if (img) {
      flushBlocks()
      const [, alt, src] = img
      if (kb) {
        out.push(<WikiImage key={key++} alt={alt} src={src.trim()} kb={kb} />)
      } else {
        // No KB context — render as a plain link (the src is not a real URL).
        out.push(
          <p key={key++} className="my-1.5 text-[14px] leading-relaxed text-muted-foreground">
            {alt || src}
          </p>,
        )
      }
      continue
    }

    // Block math: LaTeX \[ … \] display delimiters. Handle a single-line form
    // and the model's usual three-line form (\[ alone, body lines, \] alone).
    const oneLineMath = /^\\\[([\s\S]*?)\\\]$/.exec(line.trim())
    if (oneLineMath) {
      flushBlocks()
      out.push(<MathBlock key={key++} tex={oneLineMath[1].trim()} />)
      continue
    }
    if (line.trim() === '\\[') {
      flushBlocks()
      const body: string[] = []
      i++
      while (i < lines.length && lines[i].trim() !== '\\]') { body.push(lines[i]); i++ }
      out.push(<MathBlock key={key++} tex={body.join('\n').trim()} />)
      continue
    }

    // Horizontal rule / thematic break: ---, ***, or ___
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      flushBlocks()
      out.push(<hr key={key++} className="my-4 border-0 border-t border-[hsl(var(--glass-border))]" />)
      continue
    }

    // GFM table: a header row (has a `|`) IMMEDIATELY followed by a delimiter
    // row of optional-colon dashes. ONLY this header+delimiter pair triggers a
    // table — a stray `|` in prose (no delimiter line after it) stays literal.
    const tblNext = i + 1 < lines.length ? lines[i + 1].trim() : ''
    if (
      /^\s*\|?.+\|.+/.test(line) &&
      /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/.test(tblNext)
    ) {
      flushBlocks()
      // Split a row into trimmed cells on unescaped, non-code `|`. A naive
      // `split('|')` tears apart an escaped `\|` or a `|` inside an inline code
      // span; we walk the string so `` `a|b` `` and `a \| b` survive as one
      // cell. Drops the empty cell an optional leading/trailing edge pipe makes.
      const cells = (row: string) => {
        const t = row.trim()
        const c: string[] = []
        let cur = ''
        let inCode = false
        for (let j = 0; j < t.length; j++) {
          const ch = t[j]
          if (ch === '\\' && t[j + 1] === '|') { cur += '|'; j++; continue }
          if (ch === '`') { inCode = !inCode; cur += ch; continue }
          if (ch === '|' && !inCode) { c.push(cur.trim()); cur = ''; continue }
          cur += ch
        }
        c.push(cur.trim())
        if (c.length && c[0] === '') c.shift()
        if (c.length && c[c.length - 1] === '') c.pop()
        return c
      }
      const headers = cells(line)
      // Column alignment from the delimiter row: `:---`=left, `:---:`=center,
      // `---:`=right, plain `---`=unset. Applied to both header and body cells.
      const aligns = cells(tblNext).map((d) => {
        const l = d.startsWith(':'), r = d.endsWith(':')
        return l && r ? 'text-center' : r ? 'text-right' : l ? 'text-left' : ''
      })
      const alignOf = (col: number) => aligns[col] ?? ''
      // A continuation line is a body row only when it is table-SHAPED, not
      // merely a line that happens to contain a `|`. We key off the header's
      // edge-pipe style: if the header had a leading/trailing `|`, body rows
      // must match — so a trailing prose/list line with a stray `|` (and no
      // blank separator) is NOT swallowed as a phantom row.
      const hTrim = line.trim()
      const edgeLead = hTrim.startsWith('|')
      const edgeTrail = hTrim.endsWith('|')
      const isRow = (l: string) => {
        const t = l.trim()
        if (!t.includes('|')) return false
        if (edgeLead && !t.startsWith('|')) return false
        if (edgeTrail && !t.endsWith('|')) return false
        return true
      }
      // Consume header + delimiter, then all contiguous non-blank table rows.
      const body: string[][] = []
      i += 2
      while (i < lines.length && lines[i].trim() && isRow(lines[i])) {
        body.push(cells(lines[i]))
        i++
      }
      i-- // step back so the loop's i++ lands on the first post-table line (or EOF)
      out.push(
        <div key={key++} className="my-3 overflow-x-auto">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr>
                {headers.map((h, c) => (
                  <th
                    key={c}
                    className={`border border-[hsl(var(--glass-border))] bg-muted/50 px-3 py-1.5 font-semibold text-foreground ${alignOf(c)}`}
                  >
                    {inline(h, onWikiLink)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((r, ri) => (
                <tr key={ri}>
                  {headers.map((_, c) => (
                    <td
                      key={c}
                      className={`border border-[hsl(var(--glass-border))] px-3 py-1.5 text-muted-foreground ${alignOf(c)}`}
                    >
                      {inline(r[c] ?? '', onWikiLink)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }

    // Unordered item: close any open ordered list, keep accumulating the ul.
    if (line.startsWith('- ')) { flushOList(); list.push(line.slice(2)); continue }
    // Ordered item `N. …`: close any open unordered list, strip the marker, and
    // accumulate into a real <ol> (mirrors the ul buffer/flush pattern). The
    // first item's source number seeds `<ol start>` so a list beginning at N≠1
    // keeps its numbering (a blank line still ends the list).
    const om = /^(\d+)\.\s+/.exec(line)
    if (om) {
      flushList()
      if (!olist.length) olistStart = parseInt(om[1], 10)
      olist.push(line.slice(om[0].length))
      continue
    }
    flushBlocks()
    if (!line.trim()) { out.push(<div key={key++} className="h-2" />); continue }
    if (line.startsWith('### ')) out.push(<h3 key={key++} className="mt-4 mb-1.5 text-[14px] font-semibold text-foreground">{inline(line.slice(4), onWikiLink)}</h3>)
    else if (line.startsWith('## ')) out.push(<h2 key={key++} className="mt-5 mb-2 text-[16px] font-bold text-foreground">{inline(line.slice(3), onWikiLink)}</h2>)
    else if (line.startsWith('# ')) out.push(<h1 key={key++} className="mb-3 text-[22px] font-extrabold tracking-tight text-foreground">{inline(line.slice(2), onWikiLink)}</h1>)
    else if (line.startsWith('> ')) out.push(<div key={key++} className="my-2.5 border-l-2 border-amber-400/70 bg-amber-400/10 rounded-r-lg px-3 py-2 text-[13px] text-muted-foreground">{inline(line.slice(2), onWikiLink)}</div>)
    else out.push(<p key={key++} className="my-1.5 text-[14px] leading-relaxed text-muted-foreground">{inline(line, onWikiLink)}</p>)
  }
  flushBlocks()
  return <div>{out}</div>
}
