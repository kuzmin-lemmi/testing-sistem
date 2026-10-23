//! Маленький JSON: разобрать сообщение страницы (`py:{...}`) и записать строку для скрипта в странице.
//! Сообщения короткие и простые, ради них не стоит тянуть serde.
use std::iter::Peekable;
use std::str::Chars;

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(items) => items.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn str(&self, key: &str) -> Option<&str> {
        match self.get(key)? {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    /// Целое неотрицательное число (номер запуска, номер задания).
    pub fn id(&self, key: &str) -> Option<u64> {
        match self.get(key)? {
            Json::Num(n) if *n >= 0.0 && n.fract() == 0.0 && *n <= u64::MAX as f64 => Some(*n as u64),
            _ => None,
        }
    }
}

/// Глубже — уже не сообщение страницы, а мусор: не уходим в рекурсию без конца.
const MAX_DEPTH: usize = 16;

pub fn parse(text: &str) -> Option<Json> {
    let mut chars = text.chars().peekable();
    let value = value(&mut chars, 0)?;
    skip_spaces(&mut chars);
    chars.next().is_none().then_some(value)
}

fn skip_spaces(chars: &mut Peekable<Chars>) {
    while chars.next_if(|c| c.is_ascii_whitespace()).is_some() {}
}

fn value(chars: &mut Peekable<Chars>, depth: usize) -> Option<Json> {
    if depth > MAX_DEPTH {
        return None;
    }
    skip_spaces(chars);
    match *chars.peek()? {
        '{' => {
            chars.next();
            let mut items = Vec::new();
            skip_spaces(chars);
            if chars.next_if_eq(&'}').is_some() {
                return Some(Json::Obj(items));
            }
            loop {
                skip_spaces(chars);
                if chars.next()? != '"' {
                    return None;
                }
                let key = string(chars)?;
                skip_spaces(chars);
                if chars.next()? != ':' {
                    return None;
                }
                items.push((key, value(chars, depth + 1)?));
                skip_spaces(chars);
                match chars.next()? {
                    ',' => continue,
                    '}' => return Some(Json::Obj(items)),
                    _ => return None,
                }
            }
        }
        '[' => {
            chars.next();
            let mut items = Vec::new();
            skip_spaces(chars);
            if chars.next_if_eq(&']').is_some() {
                return Some(Json::Arr(items));
            }
            loop {
                items.push(value(chars, depth + 1)?);
                skip_spaces(chars);
                match chars.next()? {
                    ',' => continue,
                    ']' => return Some(Json::Arr(items)),
                    _ => return None,
                }
            }
        }
        '"' => {
            chars.next();
            string(chars).map(Json::Str)
        }
        't' => word(chars, "true", Json::Bool(true)),
        'f' => word(chars, "false", Json::Bool(false)),
        'n' => word(chars, "null", Json::Null),
        _ => {
            let mut number = String::new();
            while let Some(c) = chars.next_if(|c| c.is_ascii_digit() || "+-.eE".contains(*c)) {
                number.push(c);
            }
            number.parse().ok().map(Json::Num)
        }
    }
}

fn word(chars: &mut Peekable<Chars>, text: &str, result: Json) -> Option<Json> {
    for expected in text.chars() {
        if chars.next()? != expected {
            return None;
        }
    }
    Some(result)
}

/// Строка после открывающей кавычки.
fn string(chars: &mut Peekable<Chars>) -> Option<String> {
    let mut out = String::new();
    loop {
        match chars.next()? {
            '"' => return Some(out),
            '\\' => match chars.next()? {
                'n' => out.push('\n'),
                'r' => out.push('\r'),
                't' => out.push('\t'),
                'b' => out.push('\u{8}'),
                'f' => out.push('\u{c}'),
                'u' => {
                    let high = hex4(chars)?;
                    let code = if (0xD800..0xDC00).contains(&high) {
                        // смайлик и прочее вне BMP: JSON пишет его двумя половинками 😀
                        if chars.next()? != '\\' || chars.next()? != 'u' {
                            return None;
                        }
                        let low = hex4(chars)?;
                        if !(0xDC00..0xE000).contains(&low) {
                            return None;
                        }
                        0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)
                    } else {
                        high
                    };
                    out.push(char::from_u32(code).unwrap_or('\u{FFFD}'));
                }
                other => out.push(other), // \" \\ \/
            },
            c => out.push(c),
        }
    }
}

fn hex4(chars: &mut Peekable<Chars>) -> Option<u32> {
    let mut code = 0;
    for _ in 0..4 {
        code = code * 16 + chars.next()?.to_digit(16)?;
    }
    Some(code)
}

/// Строка в кавычках, которую можно вставить в скрипт страницы: годится и как JSON, и как строка JavaScript.
/// Экранируем все управляющие символы (вывод Python на Windows идёт с `\r\n`), U+2028/2029 (в старом
/// JavaScript это переводы строки) и `<` (чтобы `</script>` в тексте ничего не значил).
pub fn quote(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '<' => out.push_str("\\u003c"),
            c if (c as u32) < 0x20 || c == '\u{2028}' || c == '\u{2029}' => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn page_messages() {
        let run = parse(r#"{"type":"run","run":3,"task":17,"code":"print('привет')\nx = \"a\\b\"","file":{"url":"/attachments/ab.txt","name":"17.txt"}}"#).unwrap();
        assert_eq!(run.str("type"), Some("run"));
        assert_eq!(run.id("run"), Some(3));
        assert_eq!(run.id("task"), Some(17));
        assert_eq!(run.str("code"), Some("print('привет')\nx = \"a\\b\""));
        let file = run.get("file").unwrap();
        assert_eq!(file.str("name"), Some("17.txt"));

        let none = parse(r#"{"type":"run","run":1,"task":2,"code":"","file":null}"#).unwrap();
        assert_eq!(none.get("file"), Some(&Json::Null));
        assert_eq!(parse(r#" { "type" : "hello" } "#).unwrap().str("type"), Some("hello"));
    }

    #[test]
    fn unicode_escapes() {
        assert_eq!(parse(r#""\u0416\ud83d\ude00\/""#), Some(Json::Str("Ж😀/".into())));
        assert_eq!(parse(r#""\ud83d""#), None); // половинка без пары
    }

    #[test]
    fn rejects_garbage() {
        for bad in ["", "{", r#"{"a":}"#, r#"{"a":1,}"#, "[1 2]", "tru", r#"{"a":1} x"#, r#""без конца"#] {
            assert_eq!(parse(bad), None, "{bad}");
        }
        assert_eq!(parse(&"[".repeat(100)), None);
        assert_eq!(parse(r#"{"run":-1}"#).unwrap().id("run"), None);
        assert_eq!(parse(r#"{"run":1.5}"#).unwrap().id("run"), None);
    }

    #[test]
    fn quote_is_safe_for_script() {
        assert_eq!(quote("a\r\nb\t\"c\"\\"), r#""a\r\nb\t\"c\"\\""#);
        assert_eq!(quote("</script>\u{2028}\u{1}"), r#""\u003c/script>\u2028\u0001""#);
        // туда и обратно
        let text = "Привет, «мир»\r\n\u{0}😀";
        assert_eq!(parse(&quote(text)), Some(Json::Str(text.into())));
    }
}
