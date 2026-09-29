# Quantitative Finance Notebooks

Early Colab notebooks: option pricing (Black-Scholes and binomial tree) and
EUR/US government yield curves.

The larger projects that grew out of these have their own repositories:

- [portfolio-value-at-risk](https://github.com/gianni04/portfolio-value-at-risk): VaR and Expected Shortfall, historical vs parametric vs Monte Carlo
- [options-hedging-risk](https://github.com/gianni04/options-hedging-risk): delta hedging, volatility surface from CBOE indices, VaR of a hedged book
- [euro-area-yield-curve](https://github.com/gianni04/euro-area-yield-curve): ECB curves rebuilt and checked against published rates
- [on-chain-market-microstructure](https://github.com/gianni04/on-chain-market-microstructure): AMM liquidity and Kyle's lambda on Uniswap

## 1. Option pricing: Black-Scholes and binomial tree

**Notebook:** `option.ipynb`
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/gianni04/quantitative-finance-projects/blob/main/option.ipynb)

Small object-oriented pricer (`VanillaOption`, `MarketEnvironment`,
`BlackScholesPricer`, `BinomialTreePricer`):

- Black-Scholes with a dividend yield, for European calls and puts, with
  delta, gamma, vega (per 1 vol point) and theta (per day)
- Cox-Ross-Rubinstein tree (500 steps) with an early exercise check at each
  node, for American options

Example, S = K = 100, T = 1 year, r = 5%, q = 2%, vol = 20%:

```
BSM call, European    9.2270
Tree call, European   9.2231
Tree call, American   9.2231
Delta 0.5869   Gamma 0.0190   Vega 0.3790   Theta -0.0139
```

The tree converges to Black-Scholes. For this at-the-money call the early
exercise premium is zero to four decimals: with a 2% dividend yield against a
5% rate, exercising early is not worth it.

Charts: greeks with interactive sliders, American vs European prices, an
implied volatility surface and the early exercise premium surface.

## 2. EUR/US yield curves

**Notebook:** `EUR_US_Yield_Curve.ipynb`
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/gianni04/quantitative-finance-projects/blob/main/EUR_US_Yield_Curve.ipynb)

Euro area (ECB) and US Treasury (FRED) yield curves across maturities, and the
2-year / 10-year spread.

## Run

The notebooks run in Google Colab with the badges above. Locally:

```bash
pip install numpy pandas scipy yfinance matplotlib plotly ipywidgets
```
