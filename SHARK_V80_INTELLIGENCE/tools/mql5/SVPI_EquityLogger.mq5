//+------------------------------------------------------------------+
//| SVPI_EquityLogger.mq5                                            |
//| OPTIONAL, READ-ONLY helper for SHARK V80 Performance Intelligence |
//|                                                                  |
//| Writes Time,Balance,Equity every IntervalSeconds to              |
//|   <Common Files>\SVPI_equity_<login>.csv                         |
//| so SVPI can verify daily/weekly loss locks and equity drawdown.  |
//|                                                                  |
//| It places NO orders, modifies NO positions and does NOT interact |
//| with the SHARK V80 EA. Attach it to any OTHER chart.             |
//| Copy the CSV into SVPI's data\incoming folder before the weekly  |
//| run (Common Files: File > Open Data Folder > ..\..\Common\Files). |
//+------------------------------------------------------------------+
#property copyright "SVPI helper"
#property version   "1.00"
#property description "Read-only equity/balance logger for SVPI (never trades)."

input int IntervalSeconds = 60;   // sampling interval (<= 900 s for lock verification)

string g_file;

int OnInit()
  {
   g_file = StringFormat("SVPI_equity_%I64d.csv", AccountInfoInteger(ACCOUNT_LOGIN));
   if(IntervalSeconds < 10)
      return(INIT_PARAMETERS_INCORRECT);
   EventSetTimer(IntervalSeconds);
   LogNow();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTimer()
  {
   LogNow();
  }

void LogNow()
  {
   bool exists = FileIsExist(g_file, FILE_COMMON);
   int h = FileOpen(g_file, FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_COMMON, ',');
   if(h == INVALID_HANDLE)
     {
      Print("SVPI logger: cannot open ", g_file, " error ", GetLastError());
      return;
     }
   if(!exists)
      FileWrite(h, "Time", "Balance", "Equity");
   FileSeek(h, 0, SEEK_END);
   FileWrite(h,
             TimeToString(TimeTradeServer(), TIME_DATE | TIME_SECONDS),
             DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
             DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2));
   FileClose(h);
  }
//+------------------------------------------------------------------+
