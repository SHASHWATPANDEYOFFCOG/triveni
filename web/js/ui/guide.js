/* Per-screen guidance.
 *
 * An audit of the eight screens found zero tooltips and only two screens with a
 * primary action. Every number was on the page and nothing said what to do with it -
 * which is fine for someone who built it and useless for a judge with four minutes.
 *
 * So each screen declares what it is for and two or three things you can actually
 * press. The rules this follows:
 *
 *   - ONE line of purpose, then ACTIONS. Not a description of the feature; a list of
 *     things to do, in order.
 *   - Dismissible, and remembered. Instructions that cannot be turned off become
 *     furniture, and furniture is what people stop reading.
 *   - Never gone, only away. Dismissing leaves a small "show me how" chip in the same
 *     place on every screen.
 *   - Keyboard shortcuts appear here rather than only in a help dialog nobody opens.
 */

const STORE = "triveni-guides-dismissed";

/** What each screen is for, and what to press. Written as instructions, not prose. */
export const GUIDES = {
  confluence: {
    what: "Today's close, at a glance. Every mark here is proportional to a real value — the particle count is the row count, the ring is coverage, the bar widths are measured milliseconds.",
    steps: [
      "Press <strong>Close the books</strong> to re-run the reconciliation and watch each stage report over SSE.",
      "Hover the <strong>Where the time goes</strong> bar — two stages are 95% of the close.",
      "Press <strong>Review exceptions</strong> for the rows that need a person.",
    ],
  },
  stages: {
    what: "The matching ladder: five staged passes, cheapest and most certain first. Each stage only sees what the previous one could not match.",
    steps: [
      "Click any segment to see what that stage did and how many matches it added.",
      "Note the stages that add <strong>zero</strong> matches — they are drawn as hairlines rather than given a width they did not earn.",
    ],
  },
  alpha: {
    what: "The one dial that matters: how often the system is allowed to post something wrong without a human. Everything else on this screen moves when you move it.",
    steps: [
      "Press a preset — <strong>1%</strong> is the operating point the README argues for.",
      "Watch coverage and cost move <em>together</em>. Loosening the bound buys coverage and buys risk.",
      "The curve is a <strong>step function</strong>, not smooth — each point is a separately calibrated threshold.",
    ],
  },
  triage: {
    what: "The rows the system refused to decide on its own. Each carries its evidence and a sentence saying why it stopped.",
    steps: [
      "Use <span class='kbd'>j</span> / <span class='kbd'>k</span> to move through the queue.",
      "Press <span class='kbd'>e</span> to expand the evidence bundle behind a decision.",
      "Press <span class='kbd'>a</span> to accept or <span class='kbd'>r</span> to reject — both are written to the audit log.",
    ],
  },
  waterfall: {
    what: "Where one netted settlement's money actually went. A settlement is short, and “fees” is not an answer.",
    steps: [
      "Pick a settlement from the list to decompose it.",
      "Read down the deductions: gross, MDR, GST <em>on the fee</em>, TDS 194-O, refunds.",
      "Check the seal at the bottom — it balances to the paise or it says it does not.",
    ],
  },
  forecast: {
    what: "Cash landing per day on the real Indian bank calendar, including the second and fourth Saturday.",
    steps: [
      "Read the <strong>band</strong>, not the line — the line is a median, so half the time you land below it.",
      "The shortfall alert fires on the <strong>lower bound</strong>, never the point forecast.",
      "Open the model ladder to see which rungs lose to the one-line baseline. Some do, and they are published as losing.",
    ],
  },
  audit: {
    what: "The proof that nothing was altered. An append-only Merkle log, the RFC 6962 construction that secures the web PKI.",
    steps: [
      "Press <strong>Tamper with a record</strong> to corrupt one row.",
      "Watch verification fail at the <strong>exact index</strong> — and note the heads signed before it still verify, which is how the log dates the tampering.",
      "Press <strong>Restore</strong> to put it back.",
    ],
  },
  report: {
    what: "Everything measured in one page, separated into what was measured and what was assumed.",
    steps: [
      "Press <strong>Copy report</strong> to take the whole thing with you.",
      "Every figure here is reproducible: <span class='kbd'>make eval</span> regenerates it byte-identically.",
    ],
  },
};

function dismissed() {
  try {
    return new Set(JSON.parse(localStorage.getItem(STORE) ?? "[]"));
  } catch {
    return new Set();
  }
}

function remember(set) {
  try {
    localStorage.setItem(STORE, JSON.stringify([...set]));
  } catch {
    /* private mode: the guide simply reappears next visit, which is harmless */
  }
}

/**
 * Prepend the guidance strip for `screen` to `container`.
 *
 * Renders the full guide the first time, and a small restore chip afterwards. Both
 * occupy the same position on every screen, so the affordance is learnable.
 */
export function mountGuide(container, screen) {
  const guide = GUIDES[screen];
  if (!guide) return;

  const seen = dismissed();
  const node = document.createElement("div");

  const paintFull = () => {
    node.className = "guide";
    node.setAttribute("role", "note");
    node.innerHTML = `
      <div class="guide-head">
        <p><strong>How to use this screen.</strong> ${guide.what}</p>
        <button class="btn sm ghost" data-close aria-label="Hide these instructions">
          Got it
        </button>
      </div>
      <ol class="guide-steps">
        ${guide.steps.map((step) => `<li><span>${step}</span></li>`).join("")}
      </ol>`;

    node.querySelector("[data-close]").addEventListener("click", () => {
      const set = dismissed();
      set.add(screen);
      remember(set);
      paintChip();
      // Focus the restore chip, so a keyboard user is not dropped at the top of the
      // document after dismissing.
      node.querySelector("button")?.focus();
    });
  };

  const paintChip = () => {
    node.className = "";
    node.removeAttribute("role");
    node.innerHTML = `
      <button class="guide-restore" data-open>
        <span aria-hidden="true">?</span> Show me how to use this screen
      </button>`;
    node.querySelector("[data-open]").addEventListener("click", () => {
      const set = dismissed();
      set.delete(screen);
      remember(set);
      paintFull();
      node.querySelector("[data-close]")?.focus();
    });
  };

  seen.has(screen) ? paintChip() : paintFull();

  // After the screen's own heading, not above it. The title tells you where you are;
  // the guide tells you what to do once you know - putting the instructions first
  // makes every screen open on a block of text, which is the thing being fixed.
  const head = container.querySelector(".screen-head");
  head ? head.after(node) : container.prepend(node);
}

/** Forget every dismissal, so the guides come back. Wired to the help dialog. */
export function resetGuides() {
  try {
    localStorage.removeItem(STORE);
  } catch {
    /* nothing to clear */
  }
}
