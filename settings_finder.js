// ─────────────────────────────────────────────────────────────────────────────
// DEV TOOL — not imported by any code, not bundled by PyInstaller.
//
// Pins the WASM memory byte for florr's "Invert attack button" checkbox
// (Settings → Controls). Paste this whole file into the florr.io devtools
// console, then follow USAGE below. Put the address set.solve() returns into
// florr_settings.INVERT_ATTACK_ADDR.
//
// Re-run this whenever the worker log says the Invert-Attack check FAILED with
// "addr-out-of-range" or "not-bool:<n>" — that means florr shipped a new build
// and the old address no longer points at the flag.
// ─────────────────────────────────────────────────────────────────────────────

// florr.io settings-flag finder — paste in devtools console.
// Pins the memory byte for a boolean setting (e.g. "Invert attack button")
// by correlating multiple manual toggles. Background bit-churn (~300 bytes/frame)
// is rejected because only the real flag alternates in lockstep every time.
//
// USAGE
//   set.begin()                       // call with the checkbox in its CURRENT state
//   ... tick the checkbox in Settings, then:   set.mark()
//   ... untick it, then:                       set.mark()
//   ... repeat 4-6 marks, alternating on/off each time
//   set.solve()                       // -> the address(es) that flipped every time
//
// Then: set.read(addr) / set.write(addr, 0|1) to inspect or force it.

(() => {
  const M = window.Module, buf = M.asm.Mf.buffer;
  const LO = 0x400000, HI = 0x1800000;              // 4MB..24MB (covers florr's settings block ~0xAD_xxxx)
  const snap = () => new Uint8Array(buf, LO, HI - LO).slice();
  let caps = [];

  const begin = () => { caps = [snap()]; console.log('begin: 1 capture'); };
  const mark  = () => { caps.push(snap()); console.log(`mark: ${caps.length} captures`); };

  function solve() {
    if (caps.length < 4) return console.warn('need >=4 captures (begin + 3 marks)');
    const K = caps.length, n = caps[0].length, out = [];
    for (let i = 0; i < n; i++) {
      let ok = true;
      const v0 = caps[0][i];
      if (v0 > 1) continue;
      for (let k = 1; k < K; k++) {
        const v = caps[k][i];
        if (v > 1) { ok = false; break; }
        if (v === caps[k - 1][i]) { ok = false; break; }   // must change every mark
        if (v !== (k % 2 === 0 ? v0 : 1 - v0)) { ok = false; break; } // strict alternation
      }
      if (ok) out.push('0x' + (LO + i).toString(16));
    }
    console.log(`${out.length} address(es) alternate perfectly across all ${K} captures:`, out);
    return out;
  }

  const read  = (a) => new Uint8Array(buf)[typeof a === 'string' ? parseInt(a, 16) : a];
  const write = (a, v) => { new Uint8Array(buf)[typeof a === 'string' ? parseInt(a, 16) : a] = v ? 1 : 0; };

  window.set = { begin, mark, solve, read, write };
  console.log('set.* ready. set.begin() -> toggle+set.mark() x4-6 -> set.solve()');
})();
