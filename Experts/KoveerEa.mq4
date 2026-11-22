//+------------------------------------------------------------------+
//|                                                     KoveerEa.mq4 |
//| HST offline-chart watcher + Live ASD Zones Writer                |
//| Read-only chart object monitor. Does not create/delete objects.  |
//+------------------------------------------------------------------+
#property strict
#property version   "2.00"
#property description "KoveerEa: HST refresh watcher and live ASD zone recorder"

//--------------------------- WinAPI ---------------------------------
#import "user32.dll"
   int RegisterWindowMessageA(string lpString);
   int PostMessageA(int hWnd, int Msg, int wParam, int lParam);
#import

#define WM_COMMAND 0x0111
#define CHART_CMD_UPDATE_DATA 33324

//--------------------------- Inputs ---------------------------------
input int  CheckIntervalMs       = 250;
input bool EnableDebugLog        = false;
input int  ZoneScanIntervalMs    = 1000;
input string EventsFileName      = "KoveerEa_Zones_Events.csv";
input string ActiveFileName      = "KoveerEa_Zones_Active.csv";

//--------------------------- HST state ------------------------------
int    g_hwnd = 0;
int    g_mt4InternalMsg = 0;
string g_hst_filename = "";
string g_last_signature = "";

//--------------------------- Zone state -----------------------------
struct ZoneObject
{
   string   name;
   int      type;
   datetime time1;
   double   price1;
   datetime time2;
   double   price2;
   color    objColor;
   int      width;
   int      style;
};

ZoneObject g_objects[];
bool g_zone_initialized = false;
uint g_last_zone_scan_ms = 0;

//+------------------------------------------------------------------+
//| Common helpers                                                   |
//+------------------------------------------------------------------+
string DateText(datetime value)
{
   if(value <= 0) return "";
   return TimeToString(value, TIME_DATE | TIME_SECONDS);
}

string EventDateText()
{
   return TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS);
}

string ObjectTypeText(int type)
{
   if(type == OBJ_RECTANGLE) return "RECTANGLE";
   if(type == OBJ_TREND)     return "TREND";
   return IntegerToString(type);
}

bool HasSuffix(string value, string suffix)
{
   int vl = StringLen(value);
   int sl = StringLen(suffix);

   if(vl < sl) return false;

   return (StringSubstr(value, vl - sl, sl) == suffix);
}

bool IsASDObject(string name, int type)
{
   if(HasSuffix(name, "-Box-ASD") && type == OBJ_RECTANGLE)
      return true;

   if((HasSuffix(name, "-HighLine-ASD") ||
       HasSuffix(name, "-LowLine-ASD")) && type == OBJ_TREND)
      return true;

   return false;
}

//+------------------------------------------------------------------+
//| Read one matching ASD object                                     |
//+------------------------------------------------------------------+
bool ReadZoneObject(string name, ZoneObject &item)
{
   if(ObjectFind(0, name) < 0)
      return false;

   int type = (int)ObjectGetInteger(0, name, OBJPROP_TYPE);

   if(!IsASDObject(name, type))
      return false;

   item.name     = name;
   item.type     = type;
   item.time1    = (datetime)ObjectGetInteger(0, name, OBJPROP_TIME1);
   item.time2    = (datetime)ObjectGetInteger(0, name, OBJPROP_TIME2);
   item.price1   = ObjectGetDouble(0, name, OBJPROP_PRICE1);
   item.price2   = ObjectGetDouble(0, name, OBJPROP_PRICE2);
   item.objColor = (color)ObjectGetInteger(0, name, OBJPROP_COLOR);
   item.width    = (int)ObjectGetInteger(0, name, OBJPROP_WIDTH);
   item.style    = (int)ObjectGetInteger(0, name, OBJPROP_STYLE);

   return true;
}

int FindZoneIndex(ZoneObject &items[], string name)
{
   for(int i = 0; i < ArraySize(items); i++)
   {
      if(items[i].name == name)
         return i;
   }

   return -1;
}

void CopyZone(ZoneObject &dst, ZoneObject &src)
{
   dst.name     = src.name;
   dst.type     = src.type;
   dst.time1    = src.time1;
   dst.price1   = src.price1;
   dst.time2    = src.time2;
   dst.price2   = src.price2;
   dst.objColor = src.objColor;
   dst.width    = src.width;
   dst.style    = src.style;
}

bool ZoneChanged(ZoneObject &oldItem, ZoneObject &newItem)
{
   if(oldItem.type != newItem.type) return true;
   if(oldItem.time1 != newItem.time1) return true;
   if(oldItem.time2 != newItem.time2) return true;

   if(MathAbs(oldItem.price1 - newItem.price1) > 0.00000001)
      return true;

   if(MathAbs(oldItem.price2 - newItem.price2) > 0.00000001)
      return true;

   if(oldItem.objColor != newItem.objColor) return true;
   if(oldItem.width != newItem.width) return true;
   if(oldItem.style != newItem.style) return true;

   return false;
}

//+------------------------------------------------------------------+
//| CSV writing                                                      |
//+------------------------------------------------------------------+
void WriteCsvHeader(int handle)
{
   FileWrite(handle,
      "EventTime",
      "Event",
      "Symbol",
      "Period",
      "ObjectName",
      "ObjectType",
      "Time1",
      "Price1",
      "Time2",
      "Price2",
      "Color",
      "Width",
      "Style");
}

void WriteZoneRow(int handle, string eventName, ZoneObject &item)
{
   FileWrite(handle,
      EventDateText(),
      eventName,
      Symbol(),
      IntegerToString(Period()),
      item.name,
      ObjectTypeText(item.type),
      DateText(item.time1),
      DoubleToString(item.price1, 8),
      DateText(item.time2),
      DoubleToString(item.price2, 8),
      IntegerToString((int)item.objColor),
      IntegerToString(item.width),
      IntegerToString(item.style));
}

void WriteZoneEvent(string eventName, ZoneObject &item)
{
   int handle = FileOpen(
      EventsFileName,
      FILE_CSV | FILE_READ | FILE_WRITE |
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_ANSI,
      ',');

   if(handle == INVALID_HANDLE)
   {
      Print("KoveerEa: Cannot open events CSV. Error=", GetLastError());
      return;
   }

   if(FileSize(handle) == 0)
      WriteCsvHeader(handle);

   FileSeek(handle, 0, SEEK_END);

   WriteZoneRow(handle, eventName, item);

   FileFlush(handle);
   FileClose(handle);
}

void WriteActiveSnapshot(ZoneObject &items[])
{
   // Replaces only our own output CSV.
   // It does not modify chart objects.
   FileDelete(ActiveFileName);

   int handle = FileOpen(
      ActiveFileName,
      FILE_CSV | FILE_WRITE | FILE_SHARE_READ | FILE_ANSI,
      ',');

   if(handle == INVALID_HANDLE)
   {
      Print("KoveerEa: Cannot create active CSV. Error=", GetLastError());
      return;
   }

   WriteCsvHeader(handle);

   for(int i = 0; i < ArraySize(items); i++)
   {
      WriteZoneRow(handle, "ACTIVE", items[i]);
   }

   FileFlush(handle);
   FileClose(handle);
}

//+------------------------------------------------------------------+
//| Scan all chart objects and reconcile by exact object name        |
//+------------------------------------------------------------------+
void ScanASDObjects()
{
   ZoneObject current[];
   ArrayResize(current, 0);

   int chartTotal = ObjectsTotal(0, -1, -1);

   for(int i = 0; i < chartTotal; i++)
   {
      string name = ObjectName(0, i, -1, -1);

      if(name == "")
         continue;

      ZoneObject item;

      if(!ReadZoneObject(name, item))
         continue;

      int n = ArraySize(current);

      ArrayResize(current, n + 1);

      CopyZone(current[n], item);
   }

   bool changed = false;

   // New objects and updates.
   for(int c = 0; c < ArraySize(current); c++)
   {
      int oldIndex = FindZoneIndex(g_objects, current[c].name);

      if(oldIndex < 0)
      {
         WriteZoneEvent(
            g_zone_initialized ? "ADD" : "BASELINE",
            current[c]);

         changed = true;
      }
      else if(ZoneChanged(g_objects[oldIndex], current[c]))
      {
         WriteZoneEvent("UPDATE", current[c]);

         changed = true;
      }
   }

   // Previously known objects absent from this scan.
   // Each object is deleted independently.
   for(int old = 0; old < ArraySize(g_objects); old++)
   {
      if(FindZoneIndex(current, g_objects[old].name) < 0)
      {
         WriteZoneEvent("DELETE", g_objects[old]);

         changed = true;
      }
   }

   // Replace in-memory active state.
   ArrayResize(g_objects, ArraySize(current));

   for(int k = 0; k < ArraySize(current); k++)
   {
      CopyZone(g_objects[k], current[k]);
   }

   g_zone_initialized = true;

   if(changed || !FileIsExist(ActiveFileName))
   {
      WriteActiveSnapshot(g_objects);
   }
}

//+------------------------------------------------------------------+
//| HST signature reader (HST v400: 148-byte header, 44-byte bar)    |
//+------------------------------------------------------------------+
string GetFileSignature(string filename)
{
   int handle = FileOpenHistory(
      filename,
      FILE_READ | FILE_BIN | FILE_SHARE_READ);

   if(handle == INVALID_HANDLE)
      return "";

   long file_size = FileGetInteger(handle, FILE_SIZE);
   long mtime = FileGetInteger(handle, FILE_MODIFY_DATE);

   if(file_size < 148 + 44)
   {
      FileClose(handle);
      return "";
   }

   if(!FileSeek(handle, file_size - 44, SEEK_SET))
   {
      FileClose(handle);
      return "";
   }

   int bar_time = FileReadInteger(handle, INT_VALUE);

   double bar_open  = FileReadDouble(handle);
   double bar_low   = FileReadDouble(handle);
   double bar_high  = FileReadDouble(handle);
   double bar_close = FileReadDouble(handle);

   FileClose(handle);

   return StringFormat(
      "%I64d|%I64d|%d|%s|%s|%s|%s",
      mtime,
      file_size,
      bar_time,
      DoubleToString(bar_open, 12),
      DoubleToString(bar_low, 12),
      DoubleToString(bar_high, 12),
      DoubleToString(bar_close, 12));
}

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
{
   ArrayResize(g_objects, 0);

   // HSTWatcher initialization logic.
   g_hwnd = WindowHandle(Symbol(), Period());

   if(g_hwnd == 0)
   {
      Print("KoveerEa: Could not get chart window handle. Is this an offline chart?");
      return(INIT_FAILED);
   }

   g_mt4InternalMsg =
      RegisterWindowMessageA("MetaTrader4_Internal_Message");

   g_hst_filename =
      Symbol() + IntegerToString(Period()) + ".hst";

   g_last_signature = GetFileSignature(g_hst_filename);

   if(g_last_signature == "")
   {
      Print(
         "KoveerEa: Could not read HST file '",
         g_hst_filename,
         "'. Check that it exists in the MT4 history folder.");
   }
   else
   {
      Print(
         "KoveerEa started on ",
         ChartSymbol(0),
         ". Watching HST '",
         g_hst_filename,
         "'.");
   }

   int hstInterval = CheckIntervalMs;

   if(hstInterval < 50)
      hstInterval = 50;

   EventSetMillisecondTimer(hstInterval);

   int zoneInterval = ZoneScanIntervalMs;

   if(zoneInterval < 250)
      zoneInterval = 250;

   g_last_zone_scan_ms = GetTickCount();

   // Capture ASD objects present at startup.
   ScanASDObjects();

   Print("KoveerEa: HST watcher and ASD Live Zones Writer are active.");
   Print("KoveerEa CSV output folder: MQL4\\Files");

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();

   WriteActiveSnapshot(g_objects);

   Print("KoveerEa stopped. Reason=", reason);
}

//+------------------------------------------------------------------+
//| Combined timer: HST refresh + ASD scan                           |
//+------------------------------------------------------------------+
void OnTimer()
{
   // HSTWatcher logic: detect HST changes and refresh chart.
   if(g_hwnd != 0)
   {
      string current_signature =
         GetFileSignature(g_hst_filename);

      if(current_signature != "" &&
         current_signature != g_last_signature)
      {
         g_last_signature = current_signature;

         bool sent = PostMessageA(
            g_hwnd,
            WM_COMMAND,
            CHART_CMD_UPDATE_DATA,
            0);

         if(EnableDebugLog)
         {
            Print(
               "KoveerEa: HST change detected -> refresh command result=",
               sent ? "OK" : "FAIL",
               " at ",
               TimeToString(TimeLocal(), TIME_SECONDS));
         }
      }
   }

   // ASD scan runs independently of HST changes.
   int zoneInterval = ZoneScanIntervalMs;

   if(zoneInterval < 250)
      zoneInterval = 250;

   uint now = GetTickCount();

   if((uint)(now - g_last_zone_scan_ms) >= (uint)zoneInterval)
   {
      g_last_zone_scan_ms = now;

      ScanASDObjects();
   }
}

//+------------------------------------------------------------------+
//| Tick handler not required                                        |
//+------------------------------------------------------------------+
void OnTick()
{
}
//+------------------------------------------------------------------+
