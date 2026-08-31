/**
 * The gallery's rendered surfaces.
 *
 * The distinction worth pinning is that an image in a turn is drawn as an
 * image. The behaviour these replace sent the model "(no extractable text)" and
 * showed the user a filename, which is precisely the failure the gallery
 * exists to end -- so a regression here would be invisible in the API tests and
 * obvious to anyone using it.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import Gallery from "../src/Gallery.jsx";
import GalleryImages from "../src/GalleryImages.jsx";

const render = (component, props) => renderToStaticMarkup(createElement(component, props));

describe("images inside a turn", () => {
  test("an attached image is drawn as a thumbnail, not named as a file", () => {
    const html = render(GalleryImages, { items: ["abc-123"] });

    assert.match(html, /<img/);
    assert.match(html, /\/gallery\/items\/abc-123\/thumbnail/);
  });

  test("a turn carrying no image renders nothing at all", () => {
    assert.equal(render(GalleryImages, { items: [] }), "");
  });

  test("a titled item labels its thumbnail and uses its alt text", () => {
    const html = render(GalleryImages, {
      items: [{ id: "x1", title: "Approval bug", alt_text: "A misaligned button" }],
    });

    assert.match(html, /Approval bug/);
    assert.match(html, /alt="A misaligned button"/);
  });

  test("several images are all drawn", () => {
    const html = render(GalleryImages, { items: ["a", "b", "c"] });

    assert.equal(html.match(/<img/g).length, 3);
  });

  test("a caption introduces what the strip is", () => {
    const html = render(GalleryImages, { items: ["a"], caption: "Found in the gallery" });

    assert.match(html, /Found in the gallery/);
  });
});

describe("the gallery view", () => {
  test("an empty gallery explains how images get here", () => {
    const html = render(Gallery, { onBack() {} });

    assert.match(html, /paste into a chat/);
  });

  test("the search box asks what was in the image, not for a filename", () => {
    const html = render(Gallery, { onBack() {} });

    assert.match(html, /What was in it/);
  });
});
