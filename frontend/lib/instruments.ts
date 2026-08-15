// Static instrument reference (company name + primary exchange). These are stable facts, not market
// data — unknown symbols fall back to the symbol itself. No prices/indicators are ever stored here.
export interface InstrumentRef {
  company: string;
  exchange: string;
  assetClass?: string;
}

const REF: Record<string, InstrumentRef> = {
  NVDA: { company: "NVIDIA Corporation", exchange: "NASDAQ", assetClass: "Equity" },
  AAPL: { company: "Apple Inc.", exchange: "NASDAQ", assetClass: "Equity" },
  MSFT: { company: "Microsoft Corporation", exchange: "NASDAQ", assetClass: "Equity" },
  SPY: { company: "SPDR S&P 500 ETF Trust", exchange: "NYSE Arca", assetClass: "ETF" },
  QQQ: { company: "Invesco QQQ Trust", exchange: "NASDAQ", assetClass: "ETF" },
  TSLA: { company: "Tesla, Inc.", exchange: "NASDAQ", assetClass: "Equity" },
  AMZN: { company: "Amazon.com, Inc.", exchange: "NASDAQ", assetClass: "Equity" },
  META: { company: "Meta Platforms, Inc.", exchange: "NASDAQ", assetClass: "Equity" },
};

export function instrumentRef(symbol: string): InstrumentRef | null {
  if (!symbol) return null;
  return REF[symbol.toUpperCase()] ?? null;
}
