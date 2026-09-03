# ADR 0016 - A forecast ladder, a band that covers, and an alert on the lower bound

**Status:** accepted - 2026-09-03

**Context.** Loop 3 forecasts the cash *actually landing* - built from reconciled bank
credits, not gateway captures. Captures tell you what was sold; only the bank side tells
you what arrived, and payroll is paid out of the second one.

**Decision: a ladder, and the top rung has to earn its place.** Five models -
seasonal naive, seasonal drift, Theta, ETS, gradient-boosted quantile regression -
scored on *identical* rolling-origin folds. A boosted model that cannot beat "last
Tuesday" is a liability with a `.fit()` method, and the only way to know which you have
is to score them the same way.

**Measured on 200 days of history, 6 folds, 14-day horizon:**

| model | MASE | pinball@50 | verdict |
|---|---:|---:|---|
| seasonal_naive | 0.837 | 997,704 | baseline |
| seasonal_drift | 0.927 | 1,105,472 | **loses to naive** |
| theta | 0.706 | 841,563 | beats naive |
| ets | 0.598 | 713,524 | beats naive |
| **boosted** | **0.531** | **633,564** | **wins, 46.9% better than naive** |

`seasonal_drift` losing is published rather than dropped. A table where the fanciest
model always wins is a table nobody should believe.

**MASE and pinball, never MAPE.** MAPE is undefined on a zero-cash day and this series
is full of structural zeros - every Sunday, every 2nd and 4th Saturday. A metric that
cannot be computed on a third of the series is not a metric. Pinball scores the *band*
rather than the middle, which is what a liquidity decision turns on.

## Two errors that produced a band which looked fine and was not

Coverage came out at **77.4% against a nominal 90%**. Both causes were real:

1. **Residuals were taken from data the model had trained on.** A boosted model fits
   its own training rows well, so those residuals understate the error the band has to
   absorb. Fixed with *true* split conformal: fit on the first 75% of the training
   window, take residuals on the part the model has not seen, then re-fit on everything
   for the forecast itself.
2. **One pooled set of residuals sized every horizon step.** Forecast error grows with
   horizon, so day 14's band was being sized as if it were day 1's. Residuals are now
   kept and quantiled **per step**.

After both: **realised coverage 92.9% against a nominal 90%**, at a mean band width of
Rs 65,005 rather than Rs 45,611. The wider band is the honest price of actually
covering, and reporting both numbers is the point - `CoverageResult` carries nominal and
realised together and never one without the other.

A third bug in the same area was silent: propagating the baseline's score onto every
`ModelScore` used `**score.__dict__`, which is empty on a slots dataclass, so the
comparison fell back to 1.0 and labelled `seasonal_drift` (0.927) as "beats naive" when
it is worse than the 0.837 baseline. MASE scales by the *in-sample* naive error, so an
out-of-sample naive forecast does not score exactly 1.0 - comparing against the constant
rather than against the baseline's actual score is a mistake that reads as correct.

## The alert fires on the lower bound

Everyone alerts on the point forecast. It is the obvious thing and it is wrong in a
specific way: a point forecast is a *median*, so alerting on it means alerting when
there is already a 50% chance of a shortfall - too late to act. Worse, it stays silent
in exactly the case that matters most. A forecast of Rs 6,00,000 against a Rs 5,00,000
payroll looks comfortable; if the band runs from Rs 2,00,000 to Rs 10,00,000 it is a
coin toss about whether salaries clear.

So the rule is on the **lower bound of the conformal interval**: warn when the cash we
can be *confident* of falls short of what is already committed. Four levels - `watch`
when the cushion is under 15%, `warn` when the lower bound falls short, `critical` when
even the point forecast does. Commitments accumulate to the due date, because a payment
on the 20th is met by everything landing up to the 20th.

**Every output is a sentence for a human.** "Delay this vendor payment by one business
day." "Arrange Rs X of headroom." "Statutory - cannot be delayed." There is no path from
an alert to a transfer, because there is no transfer capability anywhere in Triveni to
reach.
