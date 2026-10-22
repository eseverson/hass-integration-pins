// What a pin's row says about integrations importing from its domain: `node --test tests/panel/*.test.mjs`.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.HTMLElement = class {};
globalThis.customElements = { define() {} };
const { importersNote, repinGivesAlias } = await import(
  "../../custom_components/integration_pins/panel/integration-pins-panel.js"
);

const pin = (source, alias, importers = "bundled") => ({ domain: "infrared", source, importers, override: { alias } });

test("nothing is said while importers get the pin, or nothing is known", () => {
  assert.equal(importersNote(pin("pypi", true, "pinned")), "");
  assert.equal(importersNote(pin("pypi", true, null)), "");
  // An older backend sends no importers at all.
  assert.equal(importersNote({ domain: "infrared", source: "pypi", override: {} }), "");
});

test("a pin with the alias that lost the race says what got there first", () => {
  assert.match(importersNote(pin("git", true)), /homeassistant\.components\.infrared was imported before this pin loaded/);
  assert.equal(repinGivesAlias(pin("git", true)), false);
});

test("an older pin points at whatever gives importers the pinned code", () => {
  assert.match(importersNote(pin("pypi", false)), /Re-pin to give them the pinned code/);
  assert.match(importersNote(pin("git", false)), /Re-pin/);
  assert.match(importersNote(pin("upload", false)), /Pin its folder again/);
  assert.match(importersNote(pin("adopted", false)), /keep the bundled copy/);
});

test("only a fetchable pin offers Re-pin for it", () => {
  assert.equal(repinGivesAlias(pin("pypi", false)), true);
  assert.equal(repinGivesAlias(pin("git", false)), true);
  assert.equal(repinGivesAlias(pin("upload", false)), false);
  assert.equal(repinGivesAlias(pin("adopted", false)), false);
  assert.equal(repinGivesAlias(pin("pypi", false, "pinned")), false);
});
