// Evaluates rendered policy CEL against admission fixtures, without a Kubernetes cluster.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
)

type Expression struct {
	Name       string `json:"name"`
	Expression string `json:"expression"`
}
type Policy struct {
	MatchConditions []Expression `json:"matchConditions"`
	Variables       []Expression `json:"variables"`
	Validations     []Expression `json:"validations"`
}
type Fixture struct {
	Name    string                 `json:"name"`
	Policy  Policy                 `json:"policy"`
	Input   map[string]interface{} `json:"input"`
	Allowed bool                   `json:"allowed"`
}

func main() {
	env, err := cel.NewEnv(
		cel.Variable("request", cel.DynType), cel.Variable("object", cel.DynType),
		cel.Variable("oldObject", cel.DynType), cel.Variable("namespaceObject", cel.DynType),
		cel.Variable("variables", cel.DynType),
	)
	if err != nil {
		panic(err)
	}
	programs := map[string]cel.Program{}
	eval := func(expression string, input map[string]interface{}) (interface{}, error) {
		program, exists := programs[expression]
		if !exists {
			ast, issues := env.Compile(expression)
			if issues != nil && issues.Err() != nil {
				return nil, issues.Err()
			}
			var err error
			program, err = env.Program(ast)
			if err != nil {
				return nil, err
			}
			programs[expression] = program
		}
		value, _, err := program.Eval(input)
		if err != nil {
			return nil, err
		}
		if types.IsError(value) || types.IsUnknown(value) {
			return nil, fmt.Errorf("%v", value)
		}
		return value, nil
	}
	var fixtures []Fixture
	if err := json.NewDecoder(os.Stdin).Decode(&fixtures); err != nil {
		panic(err)
	}
	failures := 0
	for _, fixture := range fixtures {
		matched, allowed := true, true
		for _, rule := range fixture.Policy.MatchConditions {
			value, err := eval(rule.Expression, fixture.Input)
			if err != nil {
				fmt.Fprintln(os.Stderr, fixture.Name, err)
				os.Exit(1)
			}
			if value != types.True {
				matched = false
			}
		}
		if matched {
			variables := map[string]interface{}{}
			fixture.Input["variables"] = variables
			for _, rule := range fixture.Policy.Variables {
				value, err := eval(rule.Expression, fixture.Input)
				if err != nil {
					fmt.Fprintln(os.Stderr, fixture.Name, err)
					os.Exit(1)
				}
				variables[rule.Name] = value
			}
			for _, rule := range fixture.Policy.Validations {
				value, err := eval(rule.Expression, fixture.Input)
				if err != nil || value != types.True {
					allowed = false
				}
			}
		}
		if allowed != fixture.Allowed {
			fmt.Fprintf(os.Stderr, "%s: allowed=%v expected=%v\n", fixture.Name, allowed, fixture.Allowed)
			failures++
		}
	}
	if failures > 0 {
		os.Exit(1)
	}
	fmt.Printf("%d rendered CEL admission cases passed\n", len(fixtures))
}
