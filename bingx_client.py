import time, hmac, hashlib, requests, json


class BingxClient:
    BASE_URL = "https://open-api.bingx.com"

    def __init__(self, api_key: str, api_secret: str, symbol: str = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.symbol = self._to_bingx_symbol(symbol) if symbol else None
        self.time_offset = self.get_server_time_offset()

    def _to_bingx_symbol(self, symbol: str) -> str:
        return symbol.replace("USDT", "-USDT")

    def _sign(self, query: str) -> str:
        return hmac.new(self.api_secret.encode("utf-8"),
                        query.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    
    def parseParam(self, paramsMap: dict) -> str:
        sortedKeys = sorted(paramsMap)
        paramsStr = "&".join(f"{k}={paramsMap[k]}" for k in sortedKeys)
        timestamp = str(int(time.time() * 1000))
        if paramsStr:
            return f"{paramsStr}&timestamp={timestamp}"
        else:
            return f"timestamp={timestamp}"
    APIURL = "https://open-api.bingx.com"
    def send_request(self, method: str, path: str, urlpa: str, payload: dict):
        sign = self._sign(urlpa)
        url = f"{self.APIURL}{path}?{urlpa}&signature={sign}"
        headers = {'X-BX-APIKEY': self.api_key}
        response = requests.request(method, url, headers=headers, data=payload)
        try:
            return response.json()  # ← сразу возвращаем dict
        except Exception as e:
            print("Ошибка при парсинге JSON:", e)
            print("Ответ сервера:", response.text)
            return None
    
    def _request(self, method: str, path: str, params=None):
        if params is None:
            params = {}
        sorted_keys = sorted(params)
        query = "&".join([f"{k}={params[k]}" for k in sorted_keys])
        signature = self._sign(query)
        url = f"{self.BASE_URL}{path}?{query}&signature={signature}"
        headers = {"X-BX-APIKEY": self.api_key}
        r = requests.request(method, url, headers=headers)
        r.raise_for_status()
        return r.json()

    def _public_request(self, path: str, params=None, timeout: int = 10):
        url = f"{self.BASE_URL}{path}"
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def get_server_time_offset(self):
        url = f"{self.BASE_URL}/openApi/swap/v2/server/time"
        r = requests.get(url)
        r.raise_for_status()
        data = r.json()
        if data.get("code") == 0:
            server_time = int(data["data"]["serverTime"])
            local_time = int(time.time() * 1000)
            return server_time - local_time
        return 0

    # ============= Полезные методы =============

    def get_mark_price(self, symbol=None):
            path = "/openApi/swap/v2/quote/premiumIndex"
            s = symbol or self.symbol
            params = {'symbol': s}
            try:
                data = self._public_request(path, params)
                if data.get('code') == 0 and 'data' in data:
                    # проверяем, что data это список
                    if isinstance(data['data'], list) and len(data['data']) > 0:
                        mark_price = data['data'][0].get('markPrice')
                        return float(mark_price) if mark_price is not None else None
                    elif isinstance(data['data'], dict):
                        mark_price = data['data'].get('markPrice')
                        return float(mark_price) if mark_price is not None else None
                return None
            except Exception as e:
                return None


    def place_market_order(self, side: str, qty: float, symbol: str = None, stop: float = None, tp: float = None, pos_side_BOTH= False):
        side_param = "BUY" if side == "long" else "SELL"
        s = symbol or self.symbol
        
        pos_side = 'LONG' if side =='long' else 'SHORT'
        pos_side = 'BOTH' if pos_side_BOTH == True else pos_side
        
        
        params = {
            "symbol": s,
            "side": side_param,
            "positionSide": "BOTH" ,
            "type": "MARKET",
            "timestamp": int(time.time()*1000) + self.get_server_time_offset(),
            "quantity": qty,
            "recvWindow": 5000,
            "timeInForce": "GTC",
        }

        # добавляем стоп, если указан
        if stop is not None:
            stopLoss_param = {
                "type": "STOP_MARKET",
                "stopPrice": stop,
                "price": stop,
                "workingType": "MARK_PRICE"
            }
            params["stopLoss"] = json.dumps(stopLoss_param)

        # добавляем тейк, если указан
        if tp is not None:
            takeProfit_param = {
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": tp,
                "price": tp,
                "workingType": "MARK_PRICE"
            }
            params["takeProfit"] = json.dumps(takeProfit_param)

        return self._request("POST", "/openApi/swap/v2/trade/order", params)

    def count_decimal_places(self, number: float) -> int:
        # Преобразуем число в строку с удалением лишних нулей после запятой
        s = str(number).rstrip('0')  
        if '.' in s:
            return len(s.split('.')[1])
        else:
            return 0
    def set_multiple_sl(self, symbol: str, qty: float, entry_price: float, side: str, sl_levels):
        precision = self.count_decimal_places(entry_price)

        if precision >= 3:
            qty_round = 0
        elif precision >= 2:
            qty_round = 1
        elif precision >= 1:
            qty_round = 2
        elif precision == 0:
            qty_round = 3
        qty_sl = round(qty / len(sl_levels), qty_round)
        print(qty_sl)
        for stop in sl_levels:
            params = {
                "symbol": symbol,
                "side": "SELL" if side == "long" else "BUY",
                "positionSide": "BOTH",
                "type": "STOP_MARKET",
                "stopPrice": stop,
                "price": stop,
                "quantity": qty_sl,
                "workingType": "MARK_PRICE",
                "timestamp": int(time.time() * 1000) + self.time_offset,
                "recvWindow": 5000
            }

            try:
                resp = self._request("POST", "/openApi/swap/v2/trade/order", params)
                print(f"[SL2] Установлен стоп: {stop}")
                
            except Exception as e:
                print(f"[SL2 ERROR] {e}")
        return resp

    def set_multiple_tp(self, symbol: str, qty: float, mark_price: float, side: str, tp_levels):
        print(mark_price)
        precision = self.count_decimal_places(mark_price)

        if side == "short":
            tp_side = "BUY"
            pos_side = "SHORT"
        else:
            tp_side = "SELL"
            pos_side = "LONG"

        answer = []
        if precision >= 3:
            qty_round = 0
        elif precision == 2:
            qty_round = 2
        elif precision == 1 :
            qty_round = 3
        elif precision == 0:
            qty_round = 4


        qty_tp = round(qty / len(tp_levels), qty_round)
        print(precision)
        print(qty_tp)
        # Тейк-профиты
        for tp in tp_levels:
            params = {
                "symbol": symbol,
                "side": tp_side,
                "positionSide": 'BOTH',
                "type": "TAKE_PROFIT_MARKET",

                "stopPrice": tp,
                "quantity": qty_tp ,
                "timestamp": int(time.time()*1000) + self.time_offset,
                "workingType": "MARK_PRICE"
            }
            try:
                resp = self._request("POST", "/openApi/swap/v2/trade/order", params)
                answer.append(resp)
                print(f"[TP] Установлен тейк-профит {tp}")
            except Exception as e:
                print("[TP ERROR]", e)

        
    
        return answer