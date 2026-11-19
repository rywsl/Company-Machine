(async () => {
  if (typeof document === "undefined") return;

  const response = await fetch("/.vite/manifest.json", {
    headers: { Accept: "application/manifest+json" },
  });
  if (!response.ok) throw new Error("MAO production manifest is unavailable");

  const manifest = await response.json();
  const entry = manifest["index.html"]?.file;
  if (typeof entry !== "string" || entry.length === 0) {
    throw new Error("MAO production entry is unavailable");
  }

  if (document.querySelector(`script[data-mao-production-entry="${entry}"]`)) return;

  const script = document.createElement("script");
  script.type = "module";
  script.src = `/${entry}`;
  script.dataset.maoProductionEntry = entry;
  document.head.appendChild(script);
})();
