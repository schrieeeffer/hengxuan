(() => {
  const currentPath = () => `${window.location.pathname}${window.location.search}${window.location.hash}`;

  document.querySelectorAll("[data-help-entry]").forEach((link) => {
    link.addEventListener("click", (event) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey
          || event.ctrlKey || event.shiftKey || event.altKey) return;
      const target = new URL(link.href, window.location.origin);
      if (target.origin !== window.location.origin) return;
      const returnTo = currentPath();
      target.searchParams.set("return_to", returnTo);
      link.href = target.toString();
      window.history.replaceState(
        { ...(window.history.state || {}), helpSource: returnTo },
        "",
      );
    });
  });

  const helpRoot = document.querySelector("[data-help-root]");
  if (!helpRoot) return;

  const returnTo = helpRoot.dataset.returnTo || "/rooms";
  window.history.replaceState(
    { ...(window.history.state || {}), helpReturnTo: returnTo },
    "",
  );

  const backLink = helpRoot.querySelector("[data-help-back]");
  backLink?.addEventListener("click", (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey
        || event.shiftKey || event.altKey || !document.referrer) return;
    try {
      const referrer = new URL(document.referrer);
      const expected = new URL(returnTo, window.location.origin);
      const referrerPath = `${referrer.pathname}${referrer.search}`;
      const expectedPath = `${expected.pathname}${expected.search}`;
      if (referrer.origin === window.location.origin && referrerPath === expectedPath) {
        event.preventDefault();
        window.history.back();
      }
    } catch (_error) {
      // The validated href remains the safe fallback.
    }
  });

  helpRoot.querySelectorAll("[data-help-topic-link]").forEach((link) => {
    link.addEventListener("click", (event) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey
          || event.ctrlKey || event.shiftKey || event.altKey) return;
      const target = new URL(link.href, window.location.origin);
      if (target.origin !== window.location.origin) return;
      event.preventDefault();
      target.searchParams.set("return_to", returnTo);
      window.location.replace(target.toString());
    });
  });
})();
