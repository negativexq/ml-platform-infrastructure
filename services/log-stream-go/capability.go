package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"regexp"
	"strings"
)

type claims struct {
	Version     int    `json:"v"`
	Namespace   string `json:"namespace"`
	Pod         string `json:"pod"`
	PodUID      string `json:"pod_uid"`
	Workflow    string `json:"workflow"`
	WorkflowUID string `json:"workflow_uid"`
	Container   string `json:"container"`
	Issued      int64  `json:"iat"`
	Expires     int64  `json:"exp"`
	End         int64  `json:"end"`
	Nonce       string `json:"nonce"`
}

var namePattern = regexp.MustCompile(`^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$`)
var noncePattern = regexp.MustCompile(`^[a-f0-9]{32}$`)

func verify(token string, key []byte, now int64) (claims, error) {
	var c claims
	invalid := errors.New("invalid capability")
	if len(token) > 4096 || len(key) < 32 {
		return c, invalid
	}
	parts := strings.Split(token, ".")
	if len(parts) != 2 {
		return c, invalid
	}
	sig, err := base64.RawURLEncoding.DecodeString(parts[1])
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(parts[0]))
	if err != nil || !hmac.Equal(sig, mac.Sum(nil)) {
		return c, invalid
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return c, invalid
	}
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&c) != nil || decoder.Decode(new(any)) != io.EOF {
		return c, invalid
	}
	if c.Version != 1 || c.Issued > now+5 || c.Expires <= now || c.Expires-c.Issued != 60 || c.End-c.Issued != 300 || c.End <= now || c.Container != "main" || !noncePattern.MatchString(c.Nonce) {
		return c, invalid
	}
	for _, name := range []string{c.Namespace, c.Pod, c.Workflow} {
		if len(name) > 253 || !namePattern.MatchString(name) {
			return c, invalid
		}
	}
	if len(c.Namespace) > 63 || !strings.HasPrefix(c.Namespace, "mlp-") || c.PodUID == "" || c.WorkflowUID == "" || len(c.PodUID) > 128 || len(c.WorkflowUID) > 128 {
		return c, invalid
	}
	return c, nil
}
