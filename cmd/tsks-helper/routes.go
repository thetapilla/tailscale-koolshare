package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/netip"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

const maxCustomRoutes = 32
const maxCustomRoutesBytes = 2048

// httpdb drops empty positional arguments. The fixed prefix makes even an
// empty list nonempty on the wire; the payload is strict, unpadded base64url.
func wireRoutes(encoded string) (string, error) {
	bad := func(reason string) (string, error) { return "", &routeError{1, "(encoded)", reason} }
	if !strings.HasPrefix(encoded, "b64.") {
		return bad("网段传输格式无效")
	}
	payload := encoded[len("b64."):]
	if len(payload) > base64.RawURLEncoding.EncodedLen(maxCustomRoutesBytes) {
		return bad("网段列表总长不得超过 2048 字节")
	}
	if strings.IndexFunc(payload, func(r rune) bool {
		return !(r >= 'A' && r <= 'Z' || r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-' || r == '_')
	}) >= 0 {
		return bad("网段传输格式无效")
	}
	decoded, err := base64.RawURLEncoding.Strict().DecodeString(payload)
	if err != nil || !utf8.Valid(decoded) {
		return bad("网段传输格式无效")
	}
	return customRoutes(string(decoded))
}

type routeError struct {
	index  int
	entry  string
	reason string
}

// Keep the shell-facing error exactly one TSV record, even for hostile input.
// The UI still treats the resulting detail as text, never HTML.
func (e *routeError) Error() string {
	entry := e.entry
	if len(entry) > 96 {
		entry = entry[:96] + "…"
	}
	if entry == "" {
		entry = "(empty)"
	}
	quoted := strconv.Quote(entry)
	entry = quoted[1 : len(quoted)-1]
	entry = strings.NewReplacer("<", `\u003c`, ">", `\u003e`, "&", `\u0026`).Replace(entry)
	return fmt.Sprintf("%d\t%s\t%s", e.index, entry, e.reason)
}

var prohibitedRoutes = []struct {
	prefix netip.Prefix
	reason string
}{
	{netip.MustParsePrefix("100.64.0.0/10"), "与 Tailscale IPv4 地址空间重叠"},
	{netip.MustParsePrefix("fd7a:115c:a1e0::/48"), "与 Tailscale IPv6 地址空间重叠（含 4via6）"},
	{netip.MustParsePrefix("127.0.0.0/8"), "与环回地址范围重叠"},
	{netip.MustParsePrefix("::1/128"), "与环回地址范围重叠"},
	{netip.MustParsePrefix("224.0.0.0/4"), "与组播地址范围重叠"},
	{netip.MustParsePrefix("ff00::/8"), "与组播地址范围重叠"},
	{netip.MustParsePrefix("169.254.0.0/16"), "与链路本地地址范围重叠"},
	{netip.MustParsePrefix("fe80::/10"), "与链路本地地址范围重叠"},
	{netip.MustParsePrefix("0.0.0.0/32"), "包含未指定地址"},
	{netip.MustParsePrefix("::/128"), "包含未指定地址"},
	{netip.MustParsePrefix("::ffff:0:0/96"), "与 IPv4 映射的 IPv6 地址范围重叠"},
}

func customRoutes(list string) (string, error) {
	if list == "" {
		return "", nil
	}
	if len(list) > maxCustomRoutesBytes {
		index := strings.Count(list[:maxCustomRoutesBytes+1], ",")
		start := strings.LastIndexByte(list[:maxCustomRoutesBytes+1], ',') + 1
		end := strings.IndexByte(list[start:], ',')
		if end < 0 {
			end = len(list) - start
		}
		return "", &routeError{index + 1, list[start : start+end], "网段列表总长不得超过 2048 字节"}
	}
	entries := strings.Split(list, ",")
	if len(entries) > maxCustomRoutes {
		return "", &routeError{maxCustomRoutes + 1, entries[maxCustomRoutes], "最多可填写 32 条网段"}
	}
	seen := make(map[netip.Prefix]bool)
	out := make([]string, 0, len(entries))
	for i, entry := range entries {
		bad := func(reason string) (string, error) { return "", &routeError{i + 1, entry, reason} }
		if entry == "" {
			return bad("网段不能为空")
		}
		if strings.IndexFunc(entry, unicode.IsSpace) >= 0 {
			return bad("网段不能包含空白字符")
		}
		if strings.IndexFunc(entry, func(r rune) bool {
			return !(r >= '0' && r <= '9' || r >= 'A' && r <= 'F' || r >= 'a' && r <= 'f' || r == '.' || r == ':' || r == '/')
		}) >= 0 {
			return bad("网段只能包含数字、a-f、冒号、点和斜杠")
		}
		p, err := netip.ParsePrefix(entry)
		if err != nil {
			return bad("不是有效的 IPv4 或 IPv6 CIDR 网段")
		}
		if p != p.Masked() {
			return bad("含主机位，应为 " + p.Masked().String())
		}
		if p.Bits() == 0 {
			return bad("默认路由请使用「提供互联网出口」")
		}
		for _, prohibited := range prohibitedRoutes {
			if p.Overlaps(prohibited.prefix) {
				return bad(prohibited.reason)
			}
		}
		if !seen[p] {
			seen[p] = true
			out = append(out, p.String())
		}
	}
	return strings.Join(out, ","), nil
}

type routeStatus struct {
	Advertised []string `json:"advertised"`
	Primary    []string `json:"primary"`
}

// Observe actual daemon preferences rather than imposing custom-input policy
// on them. Invalid elements and exit-node defaults must not enter the UI.
func statusRoutes(raw json.RawMessage) []string {
	out := []string{}
	var entries []json.RawMessage
	if json.Unmarshal(raw, &entries) != nil {
		return out
	}
	seen := make(map[netip.Prefix]bool)
	for _, entry := range entries {
		var value string
		if json.Unmarshal(entry, &value) != nil {
			continue
		}
		p, err := netip.ParsePrefix(value)
		if err != nil || p != p.Masked() || p.Bits() == 0 || seen[p] {
			continue
		}
		seen[p] = true
		out = append(out, p.String())
	}
	return out
}
