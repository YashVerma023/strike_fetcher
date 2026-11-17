# Fyers option 1-min OHLC

Streamlit page that fetches **1-minute OHLC (+ volume)** candles from the Fyers
History API for one or more **live** option contracts on a chosen date.

Instruments: NIFTY, BANKNIFTY (NSE) · SENSEX (BSE) · GOLD, GOLDM, SILVER,
SILVERM, NATURALGAS, CRUDEOIL (MCX). Edit `config/settings.json` to change the list.

## Run

```powershell
pip install -r requirements.txt
streamlit run app.py
```

Before the first fetch each day, refresh the Fyers token. The token expires
daily, and this app reads the same credentials file as the DOR project:

```powershell
cd D:\2_projects\git_projects\1DOR\fyer_code
python fyers_auth.py
```

## Using the page

The **Date** picker at the top applies to both modes. Choose where the contracts
come from with **Contracts from**:

**Pick strikes**
1. Choose index/commodity, expiry (live only), CE/PE (one or both) and strikes (one or more).
2. Click **Fetch 1-min OHLC**. Excel output: `ohlc_1min` + `summary` sheets.

**Position file**
1. Upload a compiled position file: `.csv`, or `.xlsx` (only the **first sheet**
   is read, header in row 1). Only its `Symbol` column is used; every unique
   option in it is fetched for the chosen date.
2. Before fetching, the page shows how many live contracts were found per
   index/expiry/type, and what will **not** be fetched and why.
3. Click **Fetch 1-min OHLC**. Excel output: **one sheet per index** (`NIFTY`,
   `SENSEX`, ...) + `summary`. An index whose contracts were all expired has no sheet;
   its errors are in `summary`.

Symbol spellings recognised (the same contract written several ways is fetched once):

| Spelling | Example |
|---|---|
| Fyers weekly ticker | `NIFTY26N1726750CE` (26-Nov-17, 26750 CE) |
| Fyers monthly ticker | `NIFTY26NOV25300PE`, `CRUDEOIL26NOV8800CE` |
| Spaced, 4-digit year | `SENSEX 12NOV2026 CE 81700` |
| Spaced, 2-digit year | `SENSEX 12NOV26 84500 CE` |

| Status in `summary` | Meaning |
|---|---|
| `ok` / `no data` | fetched (no data = no trades on that date, or not yet listed) |
| `error` | **expired**, not in today's symbol master, or ambiguous — not fetched |
| `skipped` | not an option (stock, future), or an index not in `config/settings.json` |

Both modes: per-contract summary, candle table, and an **Excel** download.
The last result stays on the page until the next fetch.

## Limits (by design)

| Limit | Why |
|---|---|
| Live expiries only | Fyers' History API does not serve contracts whose expiry has passed. Expired contracts need Fyers' separate Expired F&O Data API (10 intraday requests/day on the Standard plan), which this app does not use. |
| A past date only works inside the contract's life | A date before a contract was listed returns "no data". |
| ~1.3s per contract | Standard plan: 50 calls/min, and exceeding it more than 3 times in a day blocks the account for that day. |

## Configuration — `config/settings.json`

| Key | Meaning |
|---|---|
| `credentials_file` | Fyers credentials JSON (`client_id`, `access_token`). Defaults to the DOR's file. Override with the `FYERS_CREDENTIALS_FILE` env var. |
| `log_dir` | App log (`fyer_fetch_<date>.log`) and the Fyers SDK's own logs. |
| `symbol_master.*` | Download URL, cache folder, files kept per segment, timeout and retries. |
| `fyers.*` | Pacing between calls, retries, rate-limit backoff. |
| `instruments` | `name` = underlying exactly as in the Fyers symbol master; `segment` = `NSE_FO`, `BSE_FO` or `MCX_COM`. |

Use `FYER_FETCH_SETTINGS` to point to a different settings file.

## How it works

```
app.py                     Streamlit page (UI only)
fyer_fetch/
  settings.py              load + validate config/settings.json
  credentials.py           read the token, refuse an expired one or the auth code
  symbol_master.py         download the Fyers symbol master once a day (cached), list expiries/strikes, look up exact symbols
  fyers_api.py             paced, retried History API calls (ported from the DOR)
  candles.py               fetch one contract/day; dedupe, checks
  position_file.py         position .csv/.xlsx -> unique live contracts (expired -> error)
  export.py                combined table, per-index sheets, summary, Excel
  formatting.py            Indian digit grouping, strike labels
  log_setup.py, timeutil.py
```

**Symbols are looked up, not built.** Weekly and monthly symbols are spelled
differently (`NSE:NIFTY26N1717350CE` vs `NSE:BANKNIFTY26NOV31500CE`). The exact
symbol comes from the Fyers symbol master (`NSE_FO.csv`, `BSE_FO.csv`,
`MCX_COM.csv`), which lists every live contract.

**Data handling**
- **Times:** IST. Each candle is stamped with the start of its minute.
- **Same-day duplicates:** Fyers sends today's session twice. Duplicates are
  removed and the later copy is kept.
- **Today's current minute:** the still-forming candle is dropped.
- **Quality checks:** candles whose high/low don't contain open/close, zero or
  negative prices, and negative volume are flagged as warnings and kept.
- **Unusable rows:** malformed rows, unreadable rows and rows dated on another
  day are dropped and counted.
- **Missing minutes:** a minute with no trade has no candle. This is normal for
  thinly traded strikes and is not filled in.
- **MCX:** sessions run 09:00–23:55, so a full day has more candles.
