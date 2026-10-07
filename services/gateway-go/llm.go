package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/big"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

// Keep object insertion order: Python's conservative reservation uses json.dumps byte size.
type jsonField struct {
	name  string
	value any
}
type jsonObject []jsonField

var integerJSON = regexp.MustCompile(`^-?(0|[1-9][0-9]*)$`)

func parseJSON(raw []byte) (any, error) {
	if !utf8.Valid(raw) || !json.Valid(raw) {
		return nil, fmt.Errorf("invalid UTF-8")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	value, err := readJSON(decoder)
	if err != nil {
		return nil, err
	}
	if _, err = decoder.Token(); err != io.EOF {
		return nil, fmt.Errorf("trailing JSON")
	}
	return value, nil
}
func readJSON(d *json.Decoder) (any, error) {
	token, err := d.Token()
	if err != nil {
		return nil, err
	}
	delim, ok := token.(json.Delim)
	if !ok {
		return token, nil
	}
	switch delim {
	case '{':
		result := jsonObject{}
		for d.More() {
			key, err := d.Token()
			if err != nil {
				return nil, err
			}
			name, ok := key.(string)
			if !ok {
				return nil, fmt.Errorf("invalid key")
			}
			value, err := readJSON(d)
			if err != nil {
				return nil, err
			}
			result.set(name, value)
		}
		_, err = d.Token()
		return result, err
	case '[':
		result := []any{}
		for d.More() {
			value, err := readJSON(d)
			if err != nil {
				return nil, err
			}
			result = append(result, value)
		}
		_, err = d.Token()
		return result, err
	}
	return nil, fmt.Errorf("invalid JSON")
}
func (o jsonObject) get(name string) (any, bool) {
	for _, f := range o {
		if f.name == name {
			return f.value, true
		}
	}
	return nil, false
}
func (o *jsonObject) set(name string, value any) {
	for i := range *o {
		if (*o)[i].name == name {
			(*o)[i].value = value
			return
		}
	}
	*o = append(*o, jsonField{name, value})
}
func (o *jsonObject) remove(name string) {
	for i, f := range *o {
		if f.name == name {
			*o = append((*o)[:i], (*o)[i+1:]...)
			return
		}
	}
}
func truthy(v any) bool {
	switch v := v.(type) {
	case nil:
		return false
	case bool:
		return v
	case string:
		return v != ""
	case []any:
		return len(v) > 0
	case jsonObject:
		return len(v) > 0
	case json.Number:
		n, _ := strconv.ParseFloat(string(v), 64)
		return n != 0
	}
	return true
}
func numberJSON(n json.Number) string {
	if integerJSON.MatchString(string(n)) {
		v := new(big.Int)
		v.SetString(string(n), 10)
		return v.String()
	}
	f, _ := strconv.ParseFloat(string(n), 64)
	if math.IsInf(f, 1) {
		return "Infinity"
	}
	if math.IsInf(f, -1) {
		return "-Infinity"
	}
	if f != 0 && (math.Abs(f) < 1e-4 || math.Abs(f) >= 1e16) {
		return strconv.FormatFloat(f, 'e', -1, 64)
	}
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}
func quoteJSON(b *bytes.Buffer, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"', '\\':
			b.WriteByte('\\')
			b.WriteRune(r)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			if r < 32 {
				fmt.Fprintf(b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}
func pythonJSON(v any, spaces bool) []byte {
	var b bytes.Buffer
	comma, colon := ",", ":"
	if spaces {
		comma = ", "
		colon = ": "
	}
	var write func(any)
	write = func(v any) {
		switch v := v.(type) {
		case nil:
			b.WriteString("null")
		case bool:
			if v {
				b.WriteString("true")
			} else {
				b.WriteString("false")
			}
		case string:
			quoteJSON(&b, v)
		case json.Number:
			b.WriteString(numberJSON(v))
		case int:
			b.WriteString(strconv.Itoa(v))
		case jsonObject:
			b.WriteByte('{')
			for i, f := range v {
				if i > 0 {
					b.WriteString(comma)
				}
				quoteJSON(&b, f.name)
				b.WriteString(colon)
				write(f.value)
			}
			b.WriteByte('}')
		case []any:
			b.WriteByte('[')
			for i, e := range v {
				if i > 0 {
					b.WriteString(comma)
				}
				write(e)
			}
			b.WriteByte(']')
		}
	}
	write(v)
	return b.Bytes()
}
func prepareReserveChat(body []byte, name string, limit int) ([]byte, int, error) {
	fail := func(message string) ([]byte, int, error) { return nil, 0, refusal(400, "invalid_request", message) }
	parsed, err := parseJSON(body)
	if err != nil {
		return fail("the body is not JSON")
	}
	request, ok := parsed.(jsonObject)
	if !ok {
		return fail("the body is a JSON object")
	}
	messages, _ := request.get("messages")
	list, ok := messages.([]any)
	if !ok || len(list) == 0 {
		return fail(`send "messages": [{"role": "user", "content": "..."}]`)
	}
	request.set("model", name)
	if stream, _ := request.get("stream"); truthy(stream) {
		options, _ := request.get("stream_options")
		object, ok := options.(jsonObject)
		if !ok {
			object = jsonObject{}
		}
		object.set("include_usage", true)
		request.set("stream_options", object)
	}
	prompt := jsonObject{}
	for _, f := range request {
		if f.name != "max_tokens" && f.name != "max_completion_tokens" && f.name != "stream" && f.name != "stream_options" {
			prompt = append(prompt, f)
		}
	}
	estimated := len(pythonJSON(prompt, true)) + 64*len(list)
	cap := min(256, limit-estimated)
	found := false
	for _, name := range []string{"max_tokens", "max_completion_tokens"} {
		if v, ok := request.get(name); ok {
			n, ok := v.(json.Number)
			if !ok || !integerJSON.MatchString(string(n)) {
				return fail("max_tokens and max_completion_tokens must be positive integers")
			}
			i := new(big.Int)
			i.SetString(string(n), 10)
			if i.Sign() <= 0 {
				return fail("max_tokens and max_completion_tokens must be positive integers")
			}
			value := limit + 1
			if i.IsInt64() && i.Int64() <= int64(limit) {
				value = int(i.Int64())
			}
			if !found || value < cap {
				cap = value
			}
			found = true
		}
	}
	for _, name := range []string{"n", "best_of"} {
		if v, ok := request.get(name); ok {
			one := false
			switch v := v.(type) {
			case json.Number:
				n, _ := strconv.ParseFloat(string(v), 64)
				one = n == 1
			case bool:
				one = v
			}
			if !one {
				return fail("token reservations require n=1 and best_of=1")
			}
		}
	}
	if cap <= 0 || estimated+cap > limit {
		return fail("prompt and requested output exceed the per-minute token budget")
	}
	request.remove("max_tokens")
	request.remove("max_completion_tokens")
	request.set("max_tokens", cap)
	return pythonJSON(request, true), estimated + cap, nil
}

const maxMeteredBytes = 8 * 1024 * 1024

type tokenMeter struct {
	streamed           bool
	prompt, completion int
	reported, overflow bool
	pending            []byte
}

func (m *tokenMeter) feed(chunk []byte) {
	if m.overflow {
		return
	}
	if len(m.pending)+len(chunk) > maxMeteredBytes {
		m.overflow = true
		m.pending = nil
		m.reported = false
		return
	}
	m.pending = append(m.pending, chunk...)
	if m.streamed {
		for {
			i := bytes.IndexByte(m.pending, '\n')
			if i < 0 {
				break
			}
			m.event(m.pending[:i])
			m.pending = m.pending[i+1:]
		}
		if len(m.pending) == 0 {
			m.pending = nil
		}
	}
}
func (m *tokenMeter) finish() {
	if m.overflow {
		return
	}
	if m.streamed {
		m.event(m.pending)
	} else {
		m.usage(m.pending)
	}
	m.pending = nil
}
func (m *tokenMeter) event(line []byte) {
	line = bytes.TrimSpace(line)
	if bytes.HasPrefix(line, []byte("data:")) && bytes.Contains(line, []byte(`"usage"`)) {
		m.usage(bytes.TrimSpace(line[5:]))
	}
}
func (m *tokenMeter) usage(raw []byte) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var data map[string]any
	if decoder.Decode(&data) != nil {
		return
	}
	if _, err := decoder.Token(); err != io.EOF {
		return
	}
	usage, ok := data["usage"].(map[string]any)
	if !ok {
		return
	}
	read := func(name string) (int, bool) {
		n, ok := usage[name].(json.Number)
		if !ok || !integerJSON.MatchString(string(n)) {
			return 0, false
		}
		i, err := strconv.ParseInt(string(n), 10, 64)
		return int(i), err == nil && i >= 0 && i <= math.MaxInt/2
	}
	p, pok := read("prompt_tokens")
	c, cok := read("completion_tokens")
	if pok && cok {
		m.prompt = p
		m.completion = c
		m.reported = true
	}
}
