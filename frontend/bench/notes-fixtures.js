/**
 * Markdown that notes are made of, for the fidelity checks. Shared by the Node
 * tests (tests/notes-projection.test.mjs) and the browser suite
 * (bench/notes-browser.html), so both judge the same inputs.
 *
 * MARKDOWN must round-trip with its meaning intact. HTML holds what the editor
 * cannot represent exactly; each of those must either round-trip or be flagged
 * as lossy -- never change silently.
 */
export const MARKDOWN = {
  table: "| Model | Score |\n| :--- | ---: |\n| A **bold** | 3 |\n| B \\| pipe | |\n",
  nestedLists: "- one\n  - one.a\n    1. deep\n    2. deeper\n- two\n\n1. first\n2. second\n",
  looseList: "- one\n\n- two\n",
  checklists: "- [ ] open\n- [x] done\n  - [ ] nested open\n",
  orderedStart: "3. three\n4. four\n",
  code: "```python\nprint('**not bold**')\n# not a heading\n```\n\n~~~\ntilde fence\n~~~\n\nInline `code` here.\n",
  blankLines: "first\n\n\n\nafter blanks\n",
  links: "See [the docs](https://example.com/docs) and <https://neo.dev> or [mail](mailto:a@b.co).\n\n[ref link][r]\n\n[r]: https://example.com/ref\n",
  images: "![chart](https://example.com/chart.png)\n\n[report.pdf](/api/files/0b8c3c3e-1111-4222-8333-944444444444/download)\n",
  headings: "# One\n\n## Two\n\n### Three\n\n#### Four\n\nSetext\n======\n",
  quotes: "> quoted\n> - with a list\n",
  emphasis: "*one* _two_ **three** __four__ ~~five~~\n",
  rule: "above\n\n---\n\nbelow\n",
  unicode: "Café ☕ – naïve – 日本語 – emoji 🎉\n",
  hardBreak: "line one  \nline two\n",
};

export const HTML = {
  detailsBlock: "<details><summary>Sources</summary>list</details>\n\ntext\n",
  inlineSup: "Water is H<sub>2</sub>O and E=mc<sup>2</sup>.\n",
  lineBreakTag: "first<br>second\n",
  comment: "visible\n\n<!-- hidden note -->\n\nmore\n",
  underlineTag: "this is <u>underlined</u> text\n",
};
