package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"reflect"
	"strings"
	"testing"
)

func TestWireRoutes(t *testing.T) {
	for _, input := range []string{"", "192.168.60.0/24,119.188.240.179/32,2001:DB8::/64", "2001:db8::/64,2001:DB8::/64", "192.0.2.0/24,2001:db8::1/64", "<script>\nalert(1)", "\u00a0"} {
		encoded := "b64." + base64.RawURLEncoding.EncodeToString([]byte(input))
		want, wantErr := customRoutes(input)
		got, gotErr := wireRoutes(encoded)
		if got != want || fmt.Sprint(gotErr) != fmt.Sprint(wantErr) {
			t.Fatalf("wireRoutes(%q)=%q,%v; want %q,%v", encoded, got, gotErr, want, wantErr)
		}
	}
	for _, encoded := range []string{"", "b64", "B64.", "192.0.2.0/24", "b64.YQ==", "b64.YQ\n", "b64.YQ\r", "b64.YQ ", "b64.YR", "b64.A", "b64./w", "b64._w", "b64.a+b", "b64.\u00a0"} {
		got, err := wireRoutes(encoded)
		if got != "" || err == nil || err.Error() != "1\t(encoded)\t网段传输格式无效" {
			t.Fatalf("accepted invalid wire encoding %q: %q, %v", encoded, got, err)
		}
	}
	for _, size := range []int{2048, 2049} {
		_, err := wireRoutes("b64." + base64.RawURLEncoding.EncodeToString([]byte(strings.Repeat("a", size))))
		if err == nil || strings.Contains(err.Error(), "2048 字节") != (size > 2048) || len(err.Error()) > 300 {
			t.Fatalf("decoded %d byte limit: %v", size, err)
		}
	}
	if err := run([]string{"routes-wire", "b64.YQ=="}); err == nil || err.Error() != "1\t(encoded)\t网段传输格式无效" {
		t.Fatalf("wire command: %v", err)
	}
}

func TestCustomRoutes(t *testing.T) {
	for _, tt := range []struct{ input, want string }{
		{"", ""},
		{"192.168.60.0/24,119.188.240.179/32,2001:DB8:0:0::/64", "192.168.60.0/24,119.188.240.179/32,2001:db8::/64"},
		{"2001:DB8::/64,10.20.0.0/16,2001:0db8:0000::/64,10.20.0.0/16", "2001:db8::/64,10.20.0.0/16"},
		{"fd00:1234::/48,100.63.0.0/16,100.128.0.0/9,169.253.0.0/16", "fd00:1234::/48,100.63.0.0/16,100.128.0.0/9,169.253.0.0/16"},
	} {
		t.Run(tt.input, func(t *testing.T) {
			got, err := customRoutes(tt.input)
			if err != nil || got != tt.want {
				t.Fatalf("customRoutes(%q) = %q, %v; want %q", tt.input, got, err, tt.want)
			}
		})
	}
}

func TestCustomRoutesReject(t *testing.T) {
	for _, tt := range []struct{ input, reason string }{
		{"192.168.60.1/24", "含主机位，应为 192.168.60.0/24"},
		{"2001:db8::1/64", "含主机位，应为 2001:db8::/64"},
		{"0.0.0.0/0", "提供互联网出口"}, {"::/0", "提供互联网出口"},
		{"100.64.0.0/10", "Tailscale IPv4"}, {"100.0.0.0/8", "Tailscale IPv4"}, {"100.64.1.2/32", "Tailscale IPv4"},
		{"fd7a:115c:a1e0::/48", "Tailscale IPv6"}, {"fd7a:115c:a1e0:b1a:0:1:c000:200/120", "4via6"}, {"fd00::/8", "Tailscale IPv6"},
		{"127.0.0.1/32", "环回"}, {"126.0.0.0/7", "环回"}, {"::1/128", "环回"},
		{"224.0.0.0/4", "组播"}, {"192.0.0.0/2", "组播"}, {"ff00::/8", "组播"},
		{"169.254.0.0/16", "链路本地"}, {"169.0.0.0/8", "链路本地"}, {"fe80::/10", "链路本地"}, {"fe80::1/128", "链路本地"},
		{"0.0.0.0/32", "未指定"}, {"0.0.0.0/8", "未指定"}, {"::/128", "未指定"},
		{"::ffff:192.0.2.0/120", "IPv4 映射"}, {"::ffff:0:0/96", "IPv4 映射"},
		{"192.168.01.0/24", "CIDR"}, {"192.168.0.0/33", "CIDR"}, {"2001:db8::/129", "CIDR"}, {"192.0.2.1", "CIDR"},
		{" 192.0.2.0/24", "空白"}, {"192.0.2.0/24\t", "空白"}, {"192.0.2.0/24\n", "空白"}, {"192.0.2.0/24\u00a0", "空白"},
		{"192.0.2.0/24,", "不能为空"}, {",192.0.2.0/24", "不能为空"},
		{"2001:db8::%eth0/64", "只能包含"}, {"<script>alert(1)</script>", "只能包含"}, {"192.0.2.0/24\x00", "只能包含"},
	} {
		t.Run(fmt.Sprintf("%q", tt.input), func(t *testing.T) {
			got, err := customRoutes(tt.input)
			if got != "" || err == nil || !strings.Contains(err.Error(), tt.reason) {
				t.Fatalf("customRoutes(%q) = %q, %v; expected %q", tt.input, got, err, tt.reason)
			}
			if fields := strings.Split(err.Error(), "\t"); len(fields) != 3 || strings.ContainsAny(err.Error(), "\r\n<>&") {
				t.Fatalf("unsafe error record: %q", err.Error())
			}
		})
	}
}

func TestCustomRoutesLimitsAndErrorIndex(t *testing.T) {
	list := strings.TrimSuffix(strings.Repeat("192.0.2.0/24,", 32), ",")
	if got, err := customRoutes(list); err != nil || got != "192.0.2.0/24" {
		t.Fatal(got, err)
	}
	if _, err := customRoutes(list + ",192.0.2.0/24"); err == nil || !strings.HasPrefix(err.Error(), "33\t192.0.2.0/24\t") {
		t.Fatalf("33rd input must count even when duplicated: %v", err)
	}
	// Boundary checks precede CIDR parsing and errors identify the offending row.
	for _, size := range []int{2048, 2049} {
		_, err := customRoutes("192.0.2.0/24," + strings.Repeat("a", size-len("192.0.2.0/24,")))
		if err == nil || !strings.HasPrefix(err.Error(), "2\t") || strings.Contains(err.Error(), "2048 字节") != (size > 2048) {
			t.Fatalf("%d byte boundary: %v", size, err)
		}
		if len(err.Error()) > 300 {
			t.Fatalf("unbounded error length: %d", len(err.Error()))
		}
	}
	_, err := customRoutes("192.0.2.0/24,2001:db8::1/64")
	if err == nil || err.Error() != "2\t2001:db8::1/64\t含主机位，应为 2001:db8::/64" {
		t.Fatal(err)
	}
}

func TestRouteErrorReadableUnicodeAndSingleRecord(t *testing.T) {
	for _, tc := range []struct{ input, entry string }{
		{"错误网段", "错误网段"},
		{"错误网段\t第二列\r\n下一行\x00", `错误网段\t第二列\r\n下一行\x00`},
		{"错误网段\u2028下一行\u2029", `错误网段\u2028下一行\u2029`},
		{"<网段>&", `\u003c网段\u003e\u0026`},
	} {
		_, err := customRoutes("192.0.2.0/24," + tc.input)
		if err == nil {
			t.Fatalf("accepted invalid route %q", tc.input)
		}
		fields := strings.Split(err.Error(), "\t")
		if len(fields) != 3 || fields[0] != "2" || fields[1] != tc.entry || strings.ContainsAny(err.Error(), "\r\n\x00\u2028\u2029") {
			t.Fatalf("error must retain readable Unicode in one TSV record: %q", err.Error())
		}
	}
}

func TestRoutesCommand(t *testing.T) {
	for _, tt := range []struct{ input, want string }{{"", "\n"}, {"2001:DB8::/64,192.0.2.0/24", "2001:db8::/64,192.0.2.0/24\n"}, {"10.0.0.1/24", ""}} {
		reader, writer, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		original := os.Stdout
		os.Stdout = writer
		err = run([]string{"routes", tt.input})
		writer.Close()
		os.Stdout = original
		output, readErr := io.ReadAll(reader)
		reader.Close()
		if readErr != nil || string(output) != tt.want || (tt.want == "") != (err != nil) {
			t.Fatalf("%q: stdout=%q, err=%v, readErr=%v", tt.input, output, err, readErr)
		}
	}
	for _, args := range [][]string{{"routes"}, {"routes", "", "extra"}} {
		if err := run(args); err == nil {
			t.Fatalf("accepted args %q", args)
		}
	}
}

func TestStatusRoutes(t *testing.T) {
	for _, tt := range []struct {
		raw  string
		want []string
	}{
		{`["192.0.2.0/24","2001:DB8::/64","2001:db8::/64","0.0.0.0/0","::/0","192.0.2.1/24",12,null,{},"<script>","bad"]`, []string{"192.0.2.0/24", "2001:db8::/64"}},
		{`null`, []string{}}, {`{}`, []string{}}, {`"192.0.2.0/24"`, []string{}}, {`invalid`, []string{}},
	} {
		if got := statusRoutes(json.RawMessage(tt.raw)); !reflect.DeepEqual(got, tt.want) {
			t.Fatalf("statusRoutes(%s)=%v, want %v", tt.raw, got, tt.want)
		}
	}
}
