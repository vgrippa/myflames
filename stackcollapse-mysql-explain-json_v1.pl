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

# Build label with relevant fields based on operation type
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
    my $join_type = $node->{join_type} // '';
    my $join_algorithm = $node->{join_algorithm} // '';
    my $sort_fields = $node->{sort_fields};
    
    # Clean up values
    $op =~ s/`//g;
    $condition =~ s/`//g;
    $table =~ s/`//g;
    
    my @parts = ();
    
    # === NESTED LOOP JOIN ===
    # Show: operation | join_type | tables from inputs
    if ($op =~ /^Nested loop/i || $access_type eq 'join') {
        push @parts, $op;
        
        # Get table names from child inputs
        my @child_tables = get_child_tables($node);
        if (@child_tables) {
            push @parts, "tables: " . join(", ", @child_tables);
        }
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # === HASH JOIN ===
    elsif ($op =~ /^Hash join/i) {
        push @parts, $op;
        my @child_tables = get_child_tables($node);
        if (@child_tables) {
            push @parts, "tables: " . join(", ", @child_tables);
        }
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # === TABLE SCAN ===
    # Show: operation | table (alias) | rows
    elsif ($op =~ /^Table scan/i || ($access_type eq 'table' && $table)) {
        push @parts, $op;
        my $tbl_display = $table;
        $tbl_display .= " ($alias)" if $alias && $alias ne $table;
        push @parts, $tbl_display if $tbl_display && $tbl_display !~ /temporary/i;
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # === INDEX OPERATIONS ===
    # Show: operation | table.index | lookup_condition | rows × loops
    elsif ($op =~ /^Index/i || $op =~ /^Single-row/i || $op =~ /^Covering/i || $access_type eq 'index') {
        push @parts, $op;
        if ($table && $index) {
            push @parts, "$table.$index";
        } elsif ($table) {
            push @parts, $table;
        }
        if ($lookup_condition) {
            push @parts, "on: $lookup_condition";
        }
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # === FILTER ===
    # Show: operation | condition | rows
    elsif ($op =~ /^Filter/i || $access_type eq 'filter') {
        push @parts, "Filter";
        my $cond = $condition;
        $cond =~ s/DATE'(\d{4}-\d{2}-\d{2})'/$1/g;
        $cond =~ s/^\s*\(|\)\s*$//g;  # Remove outer parens
        $cond =~ s/\s+/ /g;
        $cond = substr($cond, 0, 50) . '...' if length($cond) > 50;
        push @parts, $cond if $cond;
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # === AGGREGATE ===
    # Show: operation | rows
    elsif ($op =~ /^Aggregate/i || $access_type =~ /aggregate/i) {
        push @parts, $op;
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # === SORT ===
    # Show: operation | sort_fields | rows
    elsif ($op =~ /^Sort/i || $access_type eq 'sort') {
        push @parts, "Sort";
        if ($sort_fields && ref($sort_fields) eq 'ARRAY') {
            push @parts, join(", ", @$sort_fields);
        } elsif ($op =~ /Sort:\s*(.+)$/i) {
            push @parts, $1;
        }
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # === LIMIT ===
    elsif ($op =~ /^Limit/i) {
        push @parts, $op;
    }
    
    # === MATERIALIZE ===
    elsif ($op =~ /^Materialize/i) {
        push @parts, $op;
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # === TEMPORARY TABLE ===
    elsif ($op =~ /^Temporary/i) {
        push @parts, $op;
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # === SUBQUERY ===
    elsif ($op =~ /^Subquery/i) {
        push @parts, $op;
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # === UNION ===
    elsif ($op =~ /^Union/i) {
        push @parts, $op;
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # === STREAM ===
    elsif ($op =~ /^Stream/i) {
        push @parts, $op;
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # === FALLBACK ===
    else {
        push @parts, $op;
        push @parts, format_rows_inline($rows, $loops);
    }
    
    # Build final label
    my $label = join(" | ", grep { defined $_ && $_ ne '' } @parts);
    
    # Clean up
    $label =~ s/;/_/g;
    $label =~ s/^\s+|\s+$//g;
    $label =~ s/\s+/ /g;
    $label =~ s/\|\s*\|/|/g;  # Remove empty segments
    $label =~ s/\|\s*$//;     # Remove trailing pipe
    
    return $label;
}

# Get table names from child inputs (for joins)
sub get_child_tables {
    my ($node) = @_;
    my @tables = ();
    
    return @tables unless exists $node->{inputs} && ref $node->{inputs} eq 'ARRAY';
    
    foreach my $child (@{$node->{inputs}}) {
        next unless ref $child eq 'HASH';
        
        # Get table name or alias from child
        if ($child->{alias}) {
            push @tables, $child->{alias};
        } elsif ($child->{table_name}) {
            push @tables, $child->{table_name};
        }
        # If child is a join or filter, look deeper
        elsif ($child->{operation} && $child->{operation} =~ /^(Nested|Filter|Hash)/i) {
            my @nested = get_child_tables($child);
            push @tables, @nested;
        }
    }
    
    return @tables;
}

# Format rows with loops for inline display
# Note: Format carefully to avoid numbers at end of label (confuses differential parsing)
sub format_rows_inline {
    my ($rows, $loops) = @_;
    return '' unless defined $rows;
    
    $loops //= 1;
    
    if ($loops > 1) {
        return sprintf("(rows:%.0f loops:%d)", $rows, $loops);
    } else {
        return sprintf("(rows:%.0f)", $rows);
    }
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
    
    # Round to integer (minimum 1 for root to ensure it appears)
    $time_value = int($time_value + 0.5);
    $time_value = 1 if @path == 1 && $time_value == 0 && !$use_total;
    
    # Output this node's stack
    if ($time_value > 0) {
        push @output_lines, join(";", @path) . " $time_value";
    }
}

# Start processing from root
process_node($data);

# Output results
if (@output_lines == 0) {
    warn "No valid EXPLAIN ANALYZE JSON data found.\n";
    warn "Make sure to use: EXPLAIN ANALYZE FORMAT=JSON SELECT ...\n";
    exit 1;
}

print "$_\n" for @output_lines;
