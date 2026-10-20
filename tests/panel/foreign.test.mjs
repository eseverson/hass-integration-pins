// Asking before a pin replaces a directory the panel did not install: `node --test tests/panel/*.test.mjs`.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.HTMLElement = class {};
globalThis.customElements = { define() {} };
const { foreignTarget, foreignConfirmText } = await import(
  "../../custom_components/integration_pins/panel/integration-pins-panel.js"
);

const foreign = { domain: "sun", name: "Sun Plus", version: "3.1.0", retired_dir: "integration_pins_retired" };

test("compare decides up front whether the pin needs a confirmation", () => {
  assert.deepEqual(foreignTarget({ replaces_foreign: true, foreign }), foreign);
  assert.equal(foreignTarget({ replaces_foreign: false, foreign: null }), null);
  assert.equal(foreignTarget(null), null);
  // An older backend says nothing about it, and the refusal path still covers it.
  assert.equal(foreignTarget({ added: [], changed: [] }), null);
  assert.equal(foreignTarget({ replaces_foreign: true, foreign: { domain: 7 } }), null);
});

test("a refusal's placeholders are accepted as they are", () => {
  assert.deepEqual(foreignTarget(foreign), foreign);
  assert.equal(foreignTarget({ name: "x" }), null);
});

test("the dialog names what is there and where it goes", () => {
  const text = foreignConfirmText(foreign);
  assert.match(text, /custom_components\/sun\/ holds Sun Plus 3\.1\.0/);
  assert.match(text, /did not install/);
  assert.match(text, /moves it to integration_pins_retired\//);
  assert.match(text, /nothing is deleted/i);
  assert.doesNotMatch(text, /Marketplace/);
});

test("it falls back to the domain when nothing else is known", () => {
  const text = foreignConfirmText({ domain: "sun", name: "", version: "", retired_dir: "" });
  assert.match(text, /custom_components\/sun\/ holds sun,/);
  assert.match(text, /moves it to integration_pins_retired\//);
});

test("an integration the Marketplace installed says it would try to update it too", () => {
  const text = foreignConfirmText(foreign, "someone/sun-plus");
  assert.match(text, /Marketplace installed it from someone\/sun-plus/);
  assert.match(text, /also try to update/);
});
