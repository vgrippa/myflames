#!/usr/bin/perl -w
#
# stackcollapse-mysql-explain-json.pl
#
# Converts MySQL EXPLAIN ANALYZE FORMAT=JSON output into the folded stack format
# suitable for flamegraph.pl visualization.
#
# USAGE: 
#   mysql -e "EXPLAIN ANALYZE FORMAT=JSON SELECT ..." | ./stackcollapse-mysql-explain-json.pl | ./flamegraph.pl > query.svg
#
# Or save the JSON output to a file first:
#   ./stackcollapse-mysql-explain-json.pl explain.json | ./flamegraph.pl --title "MySQL Query Plan" > query.svg
#
# Options:
#   --use-total      Use total time instead of self time (default: self time)
#   --time-unit=ms   Time unit: ms (milliseconds, default), us (microseconds), s (seconds)
#   --help           Show this help message
#
# Copyright 2024, MIT License
#

use strict;
use Getopt::Long;
use JSON::PP;  # Pure Perl JSON module (included with Perl 5.14+)

my $use_total = 0;
my $time_unit = 'ms';
my $help = 0;

GetOptions(
    'use-total'    => \$use_total,
    'time-unit=s'  => \$time_unit,
    'help'         => \$help,
) or usage();

$help && usage();

sub usage {
    die <<USAGE_END;
USAGE: $0 [options] [input_file] | flamegraph.pl > output.svg

Converts MySQL EXPLAIN ANALYZE FORMAT=JSON output to folded stack format.

Labels show: operation | access_type | table_name

Options:
    --use-total      Use total time instead of self time (default: self time)
    --time-unit=ms   Time unit: ms (default), us, or s
    --help           Show this help message

Example:
    mysql -e "EXPLAIN ANALYZE FORMAT=JSON SELECT * FROM t" | $0 | flamegraph.pl > query.svg

USAGE_END
}

# Read all input
my $json_text = do { local $/; <> };

# Strip any leading/trailing whitespace and MySQL result formatting
$json_text =~ s/^\s+//s;
$json_text =~ s/\s+$//s;

# Handle MySQL \G format output (EXPLAIN: prefix)
$json_text =~ s/^.*?EXPLAIN:\s*//s;
$json_text =~ s/^\*+.*?\*+\s*//s;

# Parse JSON
my $data;
eval {
    $data = decode_json($json_text);
};
if ($@) {
    die "Failed to parse JSON: $@\nInput was:\n$json_text\n";
}

# Build label with starts (loops) and rows - like Tanel Põder's Oracle SQL FlameGraphs
sub build_label {
    my ($node) = @_;
    
    my $op = $node->{operation} // '';
    my $access_type = $node->{access_type} // '';
    my $table = $node->{table_name} // '';
    my $alias = $node->{alias} // '';
    my $index = $node->{index_name} // '';
    my $rows = $node->{actual_rows};
    my $loops = $node->{actual_loops} // 1;
    my $condition = $node->{condition} // '';
    my $lookup_condition = $node->{lookup_condition} // '';
    
    # Clean up operation
    $op =~ s/`//g;
    $op =~ s/DATE'(\d{4}-\d{2}-\d{2})'/$1/g;
    $condition =~ s/`//g;
    $condition =~ s/DATE'(\d{4}-\d{2}-\d{2})'/$1/g;
    $table =~ s/`//g;
    
    my $label = '';
    
    # Build operation description
    if ($op =~ /^Table scan/i) {
        if ($table =~ /temporary/i) {
            $label = "TABLE SCAN <temp>";
        } else {
            my $tbl = $alias || $table;
            $label = "TABLE SCAN [$tbl]";
        }
    }
    elsif ($op =~ /^Index range scan/i) {
        my $tbl = $alias || $table;
        $label = "INDEX RANGE SCAN [$tbl.$index]";
    }
    elsif ($op =~ /^Index scan/i) {
        my $tbl = $alias || $table;
        $label = "INDEX SCAN [$tbl.$index]";
    }
    elsif ($op =~ /^Single-row index lookup/i) {
        my $tbl = $alias || $table;
        $label = "INDEX UNIQUE SCAN [$tbl.$index]";
    }
    elsif ($op =~ /^Index lookup/i) {
        my $tbl = $alias || $table;
        $label = "INDEX LOOKUP [$tbl.$index]";
    }
    elsif ($op =~ /^Covering index/i) {
        my $tbl = $alias || $table;
        $label = "COVERING INDEX [$tbl.$index]";
    }
    elsif ($op =~ /^Filter/i) {
        my $cond = $condition;
        $cond =~ s/^\s*\(|\)\s*$//g;
        $cond =~ s/\s+/ /g;
        $cond = substr($cond, 0, 40) . '..' if length($cond) > 40;
        $label = "FILTER ($cond)";
    }
    elsif ($op =~ /^Nested loop/i) {
        if ($op =~ /inner/i) {
            $label = "NESTED LOOPS";
        } elsif ($op =~ /left/i) {
            $label = "NESTED LOOPS OUTER";
        } elsif ($op =~ /semi/i) {
            $label = "NESTED LOOPS SEMI";
        } else {
            $label = "NESTED LOOPS";
        }
    }
    elsif ($op =~ /^Hash join/i) {
        $label = "HASH JOIN";
    }
    elsif ($op =~ /^Sort/i) {
        if ($op =~ /Sort:\s*(.+)$/i) {
            my $cols = $1;
            $cols = substr($cols, 0, 25) . '..' if length($cols) > 25;
            $label = "SORT [$cols]";
        } else {
            $label = "SORT";
        }
    }
    elsif ($op =~ /^Aggregate/i) {
        if ($op =~ /temporary/i) {
            $label = "AGGREGATE (temp table)";
        } else {
            $label = "AGGREGATE";
        }
    }
    elsif ($op =~ /^Limit/i) {
        if ($op =~ /Limit:\s*(\d+)/i) {
            $label = "LIMIT $1";
        } else {
            $label = "LIMIT";
        }
    }
    elsif ($op =~ /^Materialize/i) {
        $label = "MATERIALIZE";
    }
    else {
        $label = substr($op, 0, 45);
        $label .= '..' if length($op) > 45;
    }
    
    # Append starts= and rows= (like Tanel's Oracle flamegraphs)
    my @metrics = ();
    if (defined $loops && $loops > 0) {
        push @metrics, "starts=$loops";
    }
    if (defined $rows) {
        push @metrics, sprintf("rows=%.0f", $rows);
    }
    
    if (@metrics) {
        $label .= " " . join(" ", @metrics);
    }
    
    # Clean up
    $label =~ s/;/_/g;
    $label =~ s/^\s+|\s+$//g;
    
    return $label;
}


# Recursively process the execution plan tree
my @output_lines = ();

sub process_node {
    my ($node, @path) = @_;
    
    return unless ref $node eq 'HASH';
    
    # Build a clean, informative label for performance analysis
    my $operation = build_label($node);
    
    push @path, $operation;
    
    # Get timing info (in milliseconds)
    my $total_time = ($node->{actual_last_row_ms} // 0) * ($node->{actual_loops} // 1);
    
    # Calculate children's time
    my $children_time = 0;
    my @children = ();
    
    # Collect all child nodes from 'inputs' array
    if (exists $node->{inputs} && ref $node->{inputs} eq 'ARRAY') {
        @children = @{$node->{inputs}};
    }
    
    # Also check for nested operations in other common fields
    for my $field (qw(table nested_loop children)) {
        if (exists $node->{$field}) {
            if (ref $node->{$field} eq 'ARRAY') {
                push @children, @{$node->{$field}};
            } elsif (ref $node->{$field} eq 'HASH') {
                push @children, $node->{$field};
            }
        }
    }
    
    # Sort children by actual_last_row_ms (descending - slowest first)
    @children = sort {
        ($b->{actual_last_row_ms} // 0) <=> ($a->{actual_last_row_ms} // 0)
    } @children;
    
    # Process children and sum their times
    foreach my $child (@children) {
        next unless ref $child eq 'HASH';
        my $child_time = ($child->{actual_last_row_ms} // 0) * ($child->{actual_loops} // 1);
        $children_time += $child_time;
        process_node($child, @path);
    }
    
    # Calculate self time
    my $self_time = $total_time - $children_time;
    $self_time = 0 if $self_time < 0;  # Handle floating point errors
    
    # Determine which time to use
    my $time_value = $use_total ? $total_time : $self_time;
    
    # Convert time based on unit
    if ($time_unit eq 'us') {
        $time_value = $time_value * 1000;  # ms to us
    } elsif ($time_unit eq 's') {
        $time_value = $time_value / 1000;  # ms to s
    }
    
    # Store raw time value for later processing
    push @output_lines, {
        path => [@path],
        time => $time_value,
    } if $time_value > 0.0001;  # Keep anything > 0.1 microseconds
}

# Start processing from root
process_node($data);

# Output results
if (@output_lines == 0) {
    warn "No valid EXPLAIN ANALYZE JSON data found.\n";
    warn "Make sure to use: EXPLAIN ANALYZE FORMAT=JSON SELECT ...\n";
    exit 1;
}

# Check if we need to use microseconds (for sub-millisecond queries)
my $max_time = 0;
foreach my $entry (@output_lines) {
    $max_time = $entry->{time} if $entry->{time} > $max_time;
}

# If max time < 1ms, convert everything to microseconds
my $use_microseconds = ($max_time > 0 && $max_time < 1);
if ($use_microseconds) {
    warn "Note: Using microseconds (query total < 1ms)\n";
}

foreach my $entry (@output_lines) {
    my $time = $entry->{time};
    if ($use_microseconds) {
        $time = int($time * 1000 + 0.5);  # Convert to microseconds
    } else {
        $time = int($time + 0.5);
    }
    $time = 1 if $time == 0 && @{$entry->{path}} == 1;  # Ensure root appears
    next if $time <= 0;
    print join(";", @{$entry->{path}}) . " $time\n";
}
