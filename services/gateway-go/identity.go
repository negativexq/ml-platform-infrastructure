package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"math/big"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

type principal struct {
	subject string
	groups  []string
	admin   bool
}

func (p *principal) subjects() []string {
	result := []string{p.subject}
	for _, g := range p.groups {
		result = append(result, "group:"+g)
	}
	return result
}

type authenticator interface {
	Authenticate(context.Context, string) (*principal, error)
}
type signingKey struct {
	id, algorithm string
	key           any
}
type oidc struct {
	issuer, audience, usernameClaim, groupsClaim string
	admins                                       map[string]bool
	client                                       *http.Client
	mu                                           sync.Mutex
	exactIssuer, jwksURL                         string
	keys                                         []signingKey
	fetched, lastUnknownRefresh                  time.Time
}

var oidcAlgorithms = []string{"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"}

func newOIDC(issuer, audience, username, groups string, admins []string) (*oidc, error) {
	issuer = strings.TrimRight(issuer, "/")
	if !validProviderURL(issuer) || audience == "" {
		return nil, errors.New("OIDC issuer/audience configuration invalid")
	}
	if username == "" {
		username = "preferred_username"
	}
	if groups == "" {
		groups = "groups"
	}
	configured := map[string]bool{}
	for _, subject := range admins {
		if strings.TrimSpace(subject) != "" {
			configured[strings.TrimSpace(subject)] = true
		}
	}
	return &oidc{issuer: issuer, audience: audience, usernameClaim: username, groupsClaim: groups, admins: configured, client: &http.Client{Timeout: 10 * time.Second}}, nil
}
func validProviderURL(raw string) bool {
	u, err := url.Parse(raw)
	return err == nil && (u.Scheme == "http" || u.Scheme == "https") && u.Host != "" && u.User == nil && u.Fragment == ""
}
func (o *oidc) getJSON(ctx context.Context, address string, destination any) error {
	req, err := http.NewRequestWithContext(ctx, "GET", address, nil)
	if err != nil {
		return errors.New("identity provider unavailable")
	}
	response, err := o.client.Do(req)
	if err != nil {
		return errors.New("identity provider unavailable")
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return errors.New("identity provider unavailable")
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, 1024*1024+1))
	if err != nil || len(data) > 1024*1024 || json.Unmarshal(data, destination) != nil {
		return errors.New("identity provider response invalid")
	}
	return nil
}
func (o *oidc) metadata(ctx context.Context) error {
	if o.exactIssuer != "" {
		return nil
	}
	var meta struct {
		Issuer string `json:"issuer"`
		JWKS   string `json:"jwks_uri"`
	}
	if err := o.getJSON(ctx, o.issuer+"/.well-known/openid-configuration", &meta); err != nil {
		return err
	}
	if strings.TrimRight(meta.Issuer, "/") != o.issuer || !validProviderURL(meta.JWKS) {
		return errors.New("identity discovery issuer mismatch")
	}
	o.exactIssuer = meta.Issuer
	o.jwksURL = meta.JWKS
	return nil
}
func (o *oidc) refresh(ctx context.Context) error {
	var document struct {
		Keys []struct{ Kty, Kid, Alg, Use, N, E, Crv, X, Y string }
	}
	if err := o.getJSON(ctx, o.jwksURL, &document); err != nil {
		return err
	}
	keys := []signingKey{}
	for _, k := range document.Keys {
		if k.Use != "" && k.Use != "sig" {
			continue
		}
		var key any
		switch k.Kty {
		case "RSA":
			n, ne := base64.RawURLEncoding.DecodeString(k.N)
			e, ee := base64.RawURLEncoding.DecodeString(k.E)
			exp := new(big.Int).SetBytes(e)
			if ne != nil || ee != nil || len(n) == 0 || !exp.IsInt64() || exp.Int64() < 3 || exp.Int64() > 1<<31 {
				continue
			}
			key = &rsa.PublicKey{N: new(big.Int).SetBytes(n), E: int(exp.Int64())}
		case "EC":
			var curve elliptic.Curve
			switch k.Crv {
			case "P-256":
				curve = elliptic.P256()
			case "P-384":
				curve = elliptic.P384()
			default:
				continue
			}
			x, xe := base64.RawURLEncoding.DecodeString(k.X)
			y, ye := base64.RawURLEncoding.DecodeString(k.Y)
			X, Y := new(big.Int).SetBytes(x), new(big.Int).SetBytes(y)
			if xe != nil || ye != nil || !curve.IsOnCurve(X, Y) {
				continue
			}
			key = &ecdsa.PublicKey{Curve: curve, X: X, Y: Y}
		default:
			continue
		}
		keys = append(keys, signingKey{k.Kid, k.Alg, key})
	}
	if len(keys) == 0 {
		return errors.New("identity signing keys unavailable")
	}
	o.keys = keys
	o.fetched = time.Now()
	return nil
}
func (o *oidc) Authenticate(ctx context.Context, raw string) (*principal, error) {
	if len(raw) > 64*1024 {
		return nil, errors.New("invalid token")
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if err := o.metadata(ctx); err != nil {
		return nil, err
	}
	claims := jwt.MapClaims{}
	_, err := jwt.ParseWithClaims(raw, claims, func(token *jwt.Token) (any, error) {
		if len(o.keys) == 0 || time.Since(o.fetched) >= time.Hour {
			if err := o.refresh(ctx); err != nil {
				return nil, err
			}
		}
		kid, _ := token.Header["kid"].(string)
		find := func() any {
			for _, key := range o.keys {
				if (key.id == kid || (kid == "" && len(o.keys) == 1)) && (key.algorithm == "" || key.algorithm == token.Method.Alg()) {
					return key.key
				}
			}
			return nil
		}
		key := find()
		if key == nil && time.Since(o.lastUnknownRefresh) >= time.Second {
			o.lastUnknownRefresh = time.Now()
			if err := o.refresh(ctx); err != nil {
				return nil, err
			}
			key = find()
		}
		if key == nil {
			return nil, errors.New("unknown signing key")
		}
		return key, nil
	}, jwt.WithValidMethods(oidcAlgorithms), jwt.WithIssuer(o.exactIssuer), jwt.WithAudience(o.audience), jwt.WithExpirationRequired(), jwt.WithIssuedAt(), jwt.WithLeeway(30*time.Second))
	if err != nil {
		return nil, errors.New("invalid token")
	}
	sub, ok := claims["sub"].(string)
	if !ok || sub == "" {
		return nil, errors.New("the token has no stable subject")
	}
	issued, err := claims.GetIssuedAt()
	if err != nil || issued == nil {
		return nil, errors.New("invalid token")
	}
	encoded := pythonJSON([]any{o.exactIssuer, sub}, false)
	hash := sha256.Sum256(encoded)
	p := &principal{subject: "user:oidc-" + hex.EncodeToString(hash[:])}
	groups := map[string]bool{}
	if rawGroups, ok := claims[o.groupsClaim].([]any); ok {
		for _, value := range rawGroups {
			if group, ok := value.(string); ok && strings.Trim(group, "/") != "" {
				groups[strings.TrimLeft(group, "/")] = true
			}
		}
	}
	for g := range groups {
		p.groups = append(p.groups, g)
	}
	sort.Strings(p.groups)
	for _, subject := range p.subjects() {
		p.admin = p.admin || o.admins[subject]
	}
	return p, nil
}
