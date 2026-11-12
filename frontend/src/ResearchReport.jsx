import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { escapeHtml, renderMarkdown } from "./markdown.js";
import Icon from "./WorkspaceIcon.jsx";

/*
 * A research report, read the way a person reads one: the answer first, findings as prose
 * with citation pills that open their sources, the sources as cards, and the method folded
 * away at the end. The report stays Neo's own Markdown (it is also what a saved note holds);
 * this only decides how each part of it is shown.
 */

/* Section titles the report writer uses, mapped to how each is shown. */
const KIND = {
  summary: "summary",
  sources: "sources",
  "about this research": "about",
  comparison: "comparison",
  // Reports written before "About this research" existed: folded into it here.
  "what could not be established": "appendix",
  "not covered at this depth": "appendix",
  "unverified evidence": "appendix",
};

/* Strength labels the writer puts beside a finding. Most fold into the citation pill. */
const FOLDED = {
  "limited support": "Limited support: the cited passages back this only in part, or the evidence is dated.",
  "partly supported": "Partly supported: the cited passages back this only in part.",
  "partially supported": "Partly supported: the cited passages back this only in part.",
  "single source": "Single source: only one independent source states this.",
  "one source": "Single source: only one independent source states this.",
  verified: "",
};
/* These change how a finding should be read, so they stay visible in the text. */
const FLAGGED = {
  "sources disagree": "Sources disagree on this point.",
  "sources conflict": "Sources disagree on this point.",
  uncertain: "The evidence on this point is inherently uncertain.",
  "one side only": "One side only: the evidence speaks to only one side of this comparison.",
};
const LABELS = [...Object.keys(FOLDED), ...Object.keys(FLAGGED)].join("|");
// A label is "*(label)*" (rendered as <em>), "_(label)_" (older reports; the renderer leaves
// underscores alone), or bare "(label)" from summary prose.
const LABEL_FORM = `(?:<em>|_)?\\((${LABELS})\\)(?:</em>|_)?`;
const CITE_RUN = new RegExp(`(?:\\s*${LABEL_FORM})?\\s*((?:\\[\\d+\\])+)`, "g");
const LONE_LABEL = new RegExp(`\\s*(?:<em>\\((${LABELS})\\)</em>|_\\((${LABELS})\\)_)`, "g");

const NEW_SOURCE = /^(\d+)\.\s+\[(.+)\]\((\S+)\)\s*(?:—\s*(.*))?$/;
const OLD_SOURCE = /^\[(\d+)\]\s+(.*?)\s+\(([^()]*)\):\s+(\S+)\s*$/;

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function safeUrl(url) {
  return /^https?:\/\//i.test(url || "") ? url : "";
}

/**
 * A heading from a report title. New reports already drop request phrasing ("research on X");
 * older ones used the query as typed, so the same tidying is applied here.
 */
export function tidyTitle(text) {
  const value = String(text || "").trim()
    .replace(/^(?:please\s+)?(?:(?:do\s+(?:some\s+|a\s+)?)?research(?:\s+(?:on|about|into|for))?|look\s+(?:up|into)|find\s+out(?:\s+about)?|tell\s+me\s+about|investigate)\s*[:,-]?\s+/i, "")
    .replace(/\svs\.?\s/gi, " vs. ");
  return value ? value[0].toUpperCase() + value.slice(1) : "";
}

/** Title, the line under it, and the report's ## sections, in order. */
export function parseReport(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const head = [];
  const sections = [];
  let title = "";
  let current = null;
  let fenced = false;
  for (const line of lines) {
    if (/^\s*(```|~~~)/.test(line)) fenced = !fenced;
    if (!fenced && /^##\s+/.test(line)) {
      current = { title: line.replace(/^##\s+/, "").trim(), lines: [] };
      sections.push(current);
      continue;
    }
    if (!fenced && !current && !title && /^#\s+/.test(line)) {
      title = line.replace(/^#\s+/, "").trim();
      continue;
    }
    (current ? current.lines : head).push(line);
  }
  return {
    title,
    head: head.join("\n").trim(),
    sections: sections.map((section) => ({
      title: section.title,
      kind: KIND[section.title.toLowerCase()] || "body",
      body: section.lines.join("\n").trim(),
    })),
  };
}

/** The numbered source list, in either the current or the original report format. */
export function parseSources(body) {
  const sources = new Map();
  for (const raw of String(body || "").split(/\r?\n/)) {
    const line = raw.trim();
    let match = NEW_SOURCE.exec(line);
    if (match) {
      const url = safeUrl(match[3]);
      sources.set(Number(match[1]), {
        n: Number(match[1]),
        title: match[2],
        url,
        host: hostOf(url),
        detail: (match[4] || "").split(" · ").slice(1).join(" · "),
      });
      continue;
    }
    match = OLD_SOURCE.exec(line);
    if (match) {
      const url = safeUrl(match[4]);
      sources.set(Number(match[1]), {
        n: Number(match[1]),
        title: match[2],
        url,
        host: hostOf(url),
        detail: "",
      });
    }
  }
  return sources;
}

function citePill(numbers, label, sources) {
  const first = sources.get(numbers[0]);
  const name = first?.host || `Source ${numbers[0]}`;
  const note = FOLDED[label] || FLAGGED[label] || "";
  const weak = Boolean(FOLDED[label]);
  const flag = FLAGGED[label]
    ? `<span class="rs-flag">${escapeHtml(label)}</span>`
    : "";
  const more = numbers.length > 1 ? `<span class="rs-cite-more">+${numbers.length - 1}</span>` : "";
  const pill = `<button type="button" class="rs-cite${weak ? " weak" : ""}"`
    + ` data-cite="${numbers.join(",")}" data-note="${escapeHtml(note)}"`
    + ` aria-label="Sources ${numbers.join(", ")}">`
    + `<span class="rs-cite-host">${escapeHtml(name)}</span>${more}</button>`;
  // A flag never wraps away from the citation it qualifies.
  return flag ? ` <span class="rs-cite-group">${flag}${pill}</span>` : ` ${pill}`;
}

/* Older reports wrote "_italic_", which the shared renderer leaves as literal underscores. */
const UNDERSCORE_EM = /(^|[\s>(])_([^_<>\n]+?)_(?=$|[\s<.,;:!?)])/g;

/** Rendered Markdown with citations as pills, tables scrollable, comparisons as cards. */
function sectionHtml(body, kind, sources) {
  let html = renderMarkdown(body);
  // Older reports headed the query's own findings with the planner's name for them.
  const anchor = "<h3>The question as asked</h3>";
  html = html.split(anchor).length === 2 && !/<h3>/.test(html.replace(anchor, ""))
    ? html.replace(anchor, "")
    : html.replaceAll(anchor, "<h3>Overview</h3>");
  html = html
    .replace(/<table>/g, '<div class="rs-table"><table>')
    .replace(/<\/table>/g, "</table></div>")
    .replace(/<blockquote>/g, '<blockquote class="rs-callout">');
  if (kind === "comparison") {
    html = html
      .replace(/<li><strong>([^<]+)<\/strong>\s*—\s*/g, '<li><strong class="rs-cmp-name">$1</strong>')
      .replace(/<p><strong>Takeaway:<\/strong>\s*/g, '<p class="rs-takeaway"><strong>Takeaway</strong>');
  }
  return html
    .replace(CITE_RUN, (_match, label, run) => {
      const numbers = [...new Set(run.match(/\d+/g).map(Number))];
      return citePill(numbers, label || "", sources);
    })
    .replace(LONE_LABEL, (_match, emLabel, underscored) => {
      const label = emLabel || underscored;
      return FLAGGED[label] ? ` <span class="rs-flag">${escapeHtml(label)}</span>` : "";
    })
    .replace(UNDERSCORE_EM, "$1<em>$2</em>");
}

function CitationPopover({ state, sources, onEnter, onLeave }) {
  const ref = useRef(null);

  // Placed under the pill, or above it when there is no room below; kept on screen.
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node || !state) return;
    const { rect } = state;
    const width = node.offsetWidth;
    const height = node.offsetHeight;
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
    const below = rect.bottom + 6;
    const top = below + height > window.innerHeight - 8 ? Math.max(8, rect.top - height - 6) : below;
    node.style.left = `${left}px`;
    node.style.top = `${top}px`;
    node.style.visibility = "visible";
  }, [state]);

  if (!state) return null;
  // On the body, so no scrolling or clipped ancestor can cut it off.
  return createPortal(
    <div
      ref={ref}
      className="rs-cite-pop"
      role="dialog"
      aria-label="Sources for this statement"
      style={{ visibility: "hidden" }}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      {state.note && <p className="rs-cite-note">{state.note}</p>}
      {state.numbers.map((number) => {
        const source = sources.get(number);
        const body = (
          <>
            <span className="rs-cite-n">{number}</span>
            <span className="rs-cite-body">
              <strong>{source?.title || `Source ${number}`}</strong>
              <span>{[source?.host, source?.detail].filter(Boolean).join(" · ")}</span>
            </span>
          </>
        );
        return source?.url ? (
          <a className="rs-cite-src" key={number} href={source.url} target="_blank" rel="noreferrer noopener">
            {body}
            <Icon name="external" />
          </a>
        ) : (
          <div className="rs-cite-src" key={number}>{body}</div>
        );
      })}
    </div>,
    document.body,
  );
}

function SourceCards({ sources }) {
  return (
    <ol className="rs-srclist">
      {[...sources.values()].map((source) => {
        const inner = (
          <>
            <span className="rs-src-n">{source.n}</span>
            <span className="rs-src-body">
              <strong>{source.title}</strong>
              <span>{[source.host, source.detail].filter(Boolean).join(" · ")}</span>
            </span>
          </>
        );
        return (
          <li key={source.n} id={`rs-src-${source.n}`}>
            {source.url ? (
              <a className="rs-srccard" href={source.url} target="_blank" rel="noreferrer noopener">
                {inner}
                <Icon name="external" />
              </a>
            ) : (
              <span className="rs-srccard">{inner}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/** Opens and scrolls to the folded "About this research" panel, if the report has one. */
export function revealAbout() {
  const node = document.getElementById("rs-about");
  if (!node) return;
  node.open = true;
  node.scrollIntoView({ behavior: "smooth", block: "start" });
}

export default function ResearchReport({ markdown }) {
  const report = useMemo(() => parseReport(markdown), [markdown]);
  const sourcesSection = report.sections.find((section) => section.kind === "sources");
  const sources = useMemo(() => parseSources(sourcesSection?.body), [sourcesSection?.body]);

  const parts = useMemo(() => {
    const summary = report.sections.find((section) => section.kind === "summary");
    const body = report.sections.filter((section) => ["body", "comparison"].includes(section.kind));
    const about = report.sections.find((section) => section.kind === "about");
    const appendix = report.sections.filter((section) => section.kind === "appendix");
    // An older report kept its confidence and run facts under the title; they are method too.
    const legacyHead = !about && /\*\*Confidence:/.test(report.head) ? report.head : "";
    const aboutBody = [
      about?.body,
      legacyHead,
      ...appendix.map((section) => `### ${section.title}\n\n${section.body}`),
    ].filter(Boolean).join("\n\n");
    return {
      summary: summary ? sectionHtml(summary.body, "summary", sources) : "",
      body: body.map((section) => ({ ...section, html: sectionHtml(section.body, section.kind, sources) })),
      about: aboutBody ? sectionHtml(aboutBody, "about", sources) : "",
      // A report with no recognised sections (a hand-edited one, say) still renders whole.
      fallback: report.sections.length ? "" : sectionHtml(report.head, "body", sources),
    };
  }, [report, sources]);

  const [pop, setPop] = useState(null);
  const hideTimer = useRef(null);
  const containerRef = useRef(null);

  const cancelHide = useCallback(() => window.clearTimeout(hideTimer.current), []);
  const scheduleHide = useCallback(() => {
    window.clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => setPop((value) => (value?.pinned ? value : null)), 180);
  }, []);

  const openFor = useCallback((pill, pinned) => {
    window.clearTimeout(hideTimer.current);
    setPop({
      pill,
      pinned,
      numbers: pill.dataset.cite.split(",").map(Number),
      note: pill.dataset.note || "",
      rect: pill.getBoundingClientRect(),
    });
  }, []);

  // A pinned popover closes on Escape, a click elsewhere, or once the report scrolls.
  useEffect(() => {
    if (!pop) return undefined;
    const close = () => setPop(null);
    const onKey = (event) => event.key === "Escape" && close();
    const onDown = (event) => {
      if (!event.target.closest?.(".rs-cite-pop, .rs-cite")) close();
    };
    const scroller = containerRef.current?.closest(".rs-scroll");
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    scroller?.addEventListener("scroll", close, { passive: true });
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
      scroller?.removeEventListener("scroll", close);
      window.removeEventListener("resize", close);
    };
  }, [pop]);

  useEffect(() => () => window.clearTimeout(hideTimer.current), []);

  // The pill the popover belongs to reads as pressed, for pointer and screen reader alike.
  useEffect(() => {
    const pill = pop?.pill;
    if (!pill) return undefined;
    pill.setAttribute("aria-expanded", "true");
    return () => pill.setAttribute("aria-expanded", "false");
  }, [pop?.pill]);

  const pillOf = (event) => event.target.closest?.(".rs-cite");

  return (
    <div
      ref={containerRef}
      className="rs-report"
      onMouseOver={(event) => {
        const pill = pillOf(event);
        if (pill && pill !== pop?.pill) openFor(pill, false);
        else if (pill) cancelHide();
      }}
      onMouseOut={(event) => {
        const pill = pillOf(event);
        if (pill && !pill.contains(event.relatedTarget)) scheduleHide();
      }}
      onFocus={(event) => {
        const pill = pillOf(event);
        if (pill && pill !== pop?.pill) openFor(pill, false);
      }}
      onClick={(event) => {
        const pill = pillOf(event);
        if (!pill) return;
        event.preventDefault();
        if (pop?.pill === pill && pop.pinned) setPop(null);
        else openFor(pill, true);
      }}
    >
      {parts.summary && (
        <div className="rs-lede" dangerouslySetInnerHTML={{ __html: parts.summary }} />
      )}

      {parts.fallback && <div className="rs-prose" dangerouslySetInnerHTML={{ __html: parts.fallback }} />}

      {parts.body.map((section) => (
        <section className={`rs-sec rs-sec-${section.kind}`} key={section.title}>
          <h2>{section.title}</h2>
          <div className="rs-prose" dangerouslySetInnerHTML={{ __html: section.html }} />
        </section>
      ))}

      {sources.size > 0 && (
        <section className="rs-sec rs-sec-sources">
          <h2>
            Sources
            <em>{sources.size}</em>
          </h2>
          <SourceCards sources={sources} />
        </section>
      )}

      {parts.about && (
        <details className="rs-about" id="rs-about">
          <summary>
            <span>About this research</span>
            <small>Confidence, method and limits</small>
            <Icon name="next" />
          </summary>
          <div className="rs-about-body rs-prose" dangerouslySetInnerHTML={{ __html: parts.about }} />
        </details>
      )}

      <CitationPopover state={pop} sources={sources} onEnter={cancelHide} onLeave={scheduleHide} />
    </div>
  );
}
