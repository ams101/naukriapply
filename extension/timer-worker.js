// Timers in a dedicated worker aren't slowed down when the dashboard tab is in the background,
// so the 40-second gap between applications stays 40 seconds.
self.onmessage = e => {
  const { id, ms } = e.data;
  setTimeout(() => self.postMessage({ id }), ms);
};
