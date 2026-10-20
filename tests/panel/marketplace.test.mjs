// Marketplace attribution in the panel: run with `node --test tests/panel/*.test.mjs`.
// The panel is a browser module, so give it the two globals it touches when it loads.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.HTMLElement = class {};
globalThis.customElements = { define() {} };
const { marketplaceInstalled, fetchMarketplaceInstalled } = await import(
  "../../custom_components/integration_pins/panel/integration-pins-panel.js"
);

const repo = (over = {}) => ({
  full_name: "custom-components/nordpool",
  domain: "nordpool",
  category: "integration",
  installed: true,
  local_path: "/config/custom_components/nordpool",
  status: "installed",
  ...over,
});

const hassWith = (callWS, components = ["marketplace", "integration_pins"]) => ({
  config: { components },
  callWS,
});

test("an installed integration repository labels its domain", () => {
  assert.deepEqual(marketplaceInstalled([repo()]), { nordpool: "custom-components/nordpool" });
});

test("only installed integrations whose files sit at custom_components/<domain> count", () => {
  assert.deepEqual(
    marketplaceInstalled([
      repo({ installed: false }),
      repo({ installed: "true" }),
      repo({ category: "plugin" }),
      repo({ local_path: "/config/custom_components/other" }),
      repo({ local_path: null }),
      repo({ domain: "miele", full_name: "astrandb/miele", local_path: "/config/custom_components/miele/" }),
    ]),
    { miele: "astrandb/miele" },
  );
});

test("an unexpected shape gives no labels instead of wrong ones", () => {
  for (const bad of [null, undefined, "x", 3, {}, { result: [] }, [null, 1, "x", []]]) {
    assert.deepEqual(marketplaceInstalled(bad), {}, JSON.stringify(bad));
  }
  assert.deepEqual(
    marketplaceInstalled([
      repo({ domain: 7 }),
      repo({ domain: "Bad Domain" }),
      repo({ full_name: "not a repo" }),
      repo({ full_name: "a/b/c" }),
      repo({ full_name: '"><img src=x>/x' }),
    ]),
    {},
  );
});

test("without the Marketplace nothing is asked", async () => {
  let asked = 0;
  const callWS = async () => {
    asked += 1;
    return [repo()];
  };
  assert.deepEqual(await fetchMarketplaceInstalled(hassWith(callWS, ["integration_pins"])), {});
  assert.deepEqual(await fetchMarketplaceInstalled({ callWS }), {});
  assert.deepEqual(await fetchMarketplaceInstalled(null), {});
  assert.equal(asked, 0);
});

test("with the Marketplace it asks for integration repositories", async () => {
  let sent;
  const callWS = async (msg) => {
    sent = msg;
    return [repo()];
  };
  assert.deepEqual(await fetchMarketplaceInstalled(hassWith(callWS)), { nordpool: "custom-components/nordpool" });
  assert.deepEqual(sent, { type: "marketplace/repositories/list", categories: ["integration"] });
});

test("an unknown command, a refusal or any error gives no labels", async () => {
  for (const error of [
    { code: "unknown_command", message: "Unknown command." },
    { code: "unauthorized", message: "Unauthorized" },
    new Error("boom"),
  ]) {
    const callWS = async () => {
      throw error;
    };
    assert.deepEqual(await fetchMarketplaceInstalled(hassWith(callWS)), {});
  }
  const throwsAtOnce = () => {
    throw new TypeError("no connection");
  };
  assert.deepEqual(await fetchMarketplaceInstalled(hassWith(throwsAtOnce)), {});
});

test("a Marketplace that never answers is given up on quickly", async () => {
  const callWS = () => new Promise(() => {});
  const started = Date.now();
  assert.deepEqual(await fetchMarketplaceInstalled(hassWith(callWS), 50), {});
  assert.ok(Date.now() - started < 1000);
});
